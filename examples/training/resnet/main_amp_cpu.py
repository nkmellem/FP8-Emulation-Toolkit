import argparse
import os
import shutil
import time
import errno

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torchvision.models as models

import numpy as np
from mpemu import mpt_emu

try:
    from apex.fp16_utils import *
    from apex import amp, optimizers
    from apex.multi_tensor_apply import multi_tensor_applier
except ImportError:
    raise ImportError("Please install apex from https://www.github.com/nvidia/apex to run this example.")

from torch.nn.parallel import DistributedDataParallel as DDP

def fast_collate(batch, memory_format):

    imgs = [img[0] for img in batch]
    targets = torch.tensor([target[1] for target in batch], dtype=torch.int64)
    w = imgs[0].size[0]
    h = imgs[0].size[1]
    tensor = torch.zeros( (len(imgs), 3, h, w), dtype=torch.uint8).contiguous(memory_format=memory_format)
    for i, img in enumerate(imgs):
        nump_array = np.asarray(img, dtype=np.uint8)
        if(nump_array.ndim < 3):
            nump_array = np.expand_dims(nump_array, axis=-1)
        nump_array = np.rollaxis(nump_array, 2)
        #tensor[i] += torch.from_numpy(nump_array)
        tensor[i] += torch.from_numpy(np.array(nump_array))
    return tensor, targets


def parse():
    model_names = sorted(name for name in models.__dict__
                     if name.islower() and not name.startswith("__")
                     and callable(models.__dict__[name]))

    parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
    parser.add_argument('data', metavar='DIR',
                        help='path to dataset')
    parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet18',
                        choices=model_names,
                        help='model architecture: ' +
                        ' | '.join(model_names) +
                        ' (default: resnet18)')
    parser.add_argument('-j', '--workers', default=1, type=int, metavar='N',
                        help='number of data loading workers (default: 4)')
    parser.add_argument('--epochs', default=90, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                        help='manual epoch number (useful on restarts)')
    parser.add_argument('-b', '--batch-size', default=256, type=int,
                        metavar='N', help='mini-batch size per process (default: 256)')
    parser.add_argument('--lr', '--learning-rate', default=0.1, type=float,
                        metavar='LR', help='Initial learning rate.  Will be scaled by <global batch size>/256: args.lr = args.lr*float(args.batch_size*args.world_size)/256.  A warmup schedule will also be applied over the first 5 epochs.')
    parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--weight-decay', '--wd', default=5e-5, type=float,
                        metavar='W', help='weight decay (default: 5e-5)')
    parser.add_argument('--print-freq', '-p', default=10, type=int,
                        metavar='N', help='print frequency (default: 10)')
    parser.add_argument('--resume', default='', type=str, metavar='PATH',
                        help='path to latest checkpoint (default: none)')
    parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                        help='evaluate model on validation set')
    parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                        help='use pre-trained model')

    parser.add_argument('--prof', default=-1, type=int,
                        help='Only run 10 iterations for profiling.')
    parser.add_argument('--deterministic', action='store_true')

    parser.add_argument("--local_rank", default=0, type=int)
    parser.add_argument('--sync_bn', action='store_true',
                        help='enabling apex sync BN.')
    parser.add_argument('--output-dir', default='.', help='path where to save')
    # Learning rate schedule related
    parser.add_argument('--lr-mode', type=str, default='cosine', choices=['step', 'poly', 'cosine'], help="LR change dynamics")
    parser.add_argument('--warmup-mode', type=str, default='linear', choices=['linear', 'constant'], help="Warmup LR dynamics")
    parser.add_argument('--warmup-epochs', type=int, default=5, help="Number of epochs for warmup")

    parser.add_argument('--opt-level', default="O2", type=str)
    parser.add_argument('--keep-batchnorm-fp32', default="True", type=str)
    parser.add_argument('--loss-scale', default="dynamic", type=str)
    parser.add_argument('--channels-last', type=bool, default=False)
    parser.add_argument('--fp8-training', action='store_true', default=False)
    parser.add_argument('--fp8-algo', default="direct", type=str, help='Supported algos : direct, hybrid')
    parser.add_argument('--master-weight-precision', default='fp32', type=str,
    	                    help='Select master copy precision.(Options: fp32, fp16, bf8)')
    parser.add_argument('--patch-ops', default='None', type=str,
                        help='Patch Ops to enable bit acurate emulation of TMUL')
    parser.add_argument('--pruning-algo', default="none", type=str,
            help='select the pruning method applied to training or inference, available options : fine-grained, unstructured, adaptive')

    args = parser.parse_args()
    return args

def main():
    global best_prec1, args

    args = parse()

    
    print("\nMKL available?: {}\n".format(torch.backends.mkl.is_available()))
    print("\nMKLDNN available?: {}\n".format(torch.backends.mkldnn.is_available()))
    print("\nOpenMP enabled?: {}\n".format(torch.backends.openmp.is_available()))

    best_prec1 = 0
    if args.deterministic:
        torch.manual_seed(args.local_rank)
        torch.set_printoptions(precision=10)

    args.distributed = False
    if 'WORLD_SIZE' in os.environ:
        args.distributed = int(os.environ['WORLD_SIZE']) > 1

    args.gpu = 0
    args.world_size = int(os.environ['WORLD_SIZE'])

    print("world_size : local_rank : " , args.world_size, args.local_rank)
    if args.distributed:
        args.gpu = args.local_rank
        torch.distributed.init_process_group(backend='gloo',
                                             init_method='env://')
        args.world_size = torch.distributed.get_world_size()

    print("world_size : " , args.world_size)

    if args.channels_last:
        memory_format = torch.channels_last
    else:
        memory_format = torch.contiguous_format

    if args.output_dir:
        try:
            os.makedirs(args.output_dir)
        except OSError as e:
            if e.errno != errno.EEXIST:
               raise

    if args.lr_mode == 'step':
        args.weight_decay = 1e-4;

    # create model
    if args.pretrained:
        print("=> using pre-trained model '{}'".format(args.arch))
        model = models.__dict__[args.arch](pretrained=True)
    else:
        print("=> creating model '{}'".format(args.arch))
        model = models.__dict__[args.arch]()

    model = model.to('cpu')

    if args.sync_bn:
        import apex
        print("using apex synced BN")
        model = apex.parallel.convert_syncbn_model(model)

    model = model.to(memory_format=memory_format)

    # Scale learning rate based on global batch size
    args.lr = args.lr*float(args.batch_size*args.world_size)/256.
    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)

    if args.fp8_training:
        args.opt_level = "O4"

        print("opt_level = {}".format(args.opt_level))
        print("keep_batchnorm_fp32 = {}".format(args.keep_batchnorm_fp32), type(args.keep_batchnorm_fp32))
        print("loss_scale = {}".format(args.loss_scale), type(args.loss_scale))

        # Initialize Amp.  Amp accepts either values or strings for the optional override arguments,
        # for convenient interoperation with argparse.
        model, optimizer = amp.initialize(model, optimizer,
                                      opt_level=args.opt_level,
                                      keep_batchnorm_fp32=args.keep_batchnorm_fp32,
                                      loss_scale=args.loss_scale
                                      )

        emulator = None
        list_exempt_layers = ["conv1","bn1","fc"]
        list_layers_output_fused = None
        model, emulator = mpt_emu.initialize(model, optimizer, training_algo=args.fp8_algo, 
                              hw_patch=args.patch_ops, pruning_algo=args.pruning_algo,
                              list_exempt_layers=list_exempt_layers, list_layers_output_fused=list_layers_output_fused, 
                              device="cpu", verbose=True)

        print(model)
        
    if args.distributed:
        model = DDP(model)

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cpu()
    # Optionally resume from a checkpoint
    if args.resume:
        # Use a local scope to avoid dangling references
        def resume():
            if os.path.isfile(args.resume):
                print("=> loading checkpoint '{}'".format(args.resume))
                checkpoint = torch.load(args.resume, map_location=torch.device('cpu'))
                args.start_epoch = checkpoint['epoch']
                best_prec1 = checkpoint['best_prec1']
                model.load_state_dict(checkpoint['state_dict'])
                optimizer.load_state_dict(checkpoint['optimizer'])
                print("=> loaded checkpoint '{}' (epoch {})"
                      .format(args.resume, checkpoint['epoch']))
            else:
                print("=> no checkpoint found at '{}'".format(args.resume))
        resume()

    # Data loading code
    traindir = os.path.join(args.data, 'train')
    valdir = os.path.join(args.data, 'val')

    if(args.arch == "inception_v3"):
        raise RuntimeError("Currently, inception_v3 is not supported by this example.")
        # crop_size = 299
        # val_size = 320 # I chose this value arbitrarily, we can adjust.
    else:
        crop_size = 224
        val_size = 256

    train_dataset = datasets.ImageFolder(
        traindir,
        transforms.Compose([
            transforms.RandomResizedCrop(crop_size),
            transforms.RandomHorizontalFlip(),
        ]))
    val_dataset = datasets.ImageFolder(valdir, transforms.Compose([
            transforms.Resize(val_size),
            transforms.CenterCrop(crop_size),
        ]))

    train_sampler = None
    val_sampler = None
    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
        val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset)

    collate_fn = lambda b: fast_collate(b, memory_format)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.batch_size, shuffle=(train_sampler is None),
        num_workers=args.workers, pin_memory=False, sampler=train_sampler, collate_fn=collate_fn)

    val_loader = torch.utils.data.DataLoader(
        val_dataset,
        batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=False,
        sampler=val_sampler,
        collate_fn=collate_fn)

    if args.evaluate:
        validate(val_loader, model, criterion)
        return

    from lr_scheduler import LRScheduler
    lr_scheduler = LRScheduler(optimizer, len(train_loader), args)

    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)

        if emulator is not None :
            emulator.update_global_steps(epoch*len(train_loader))
        # train for one epoch
        train(train_loader, model, criterion, optimizer, emulator, epoch, lr_scheduler)

        # evaluate on validation set
        prec1 = validate(val_loader, model, criterion)

        # remember best prec@1 and save checkpoint
        if args.local_rank == 0:
            is_best = prec1 > best_prec1
            best_prec1 = max(prec1, best_prec1)
            save_checkpoint({
                'epoch': epoch + 1,
                'arch': args.arch,
                'state_dict': model.state_dict(),
                'best_prec1': best_prec1,
                'optimizer' : optimizer.state_dict(),
            }, is_best, output_dir=args.output_dir)

class data_prefetcher():
    def __init__(self, loader):
        self.loader = iter(loader)
        self.stream = None #torch.cuda.Stream()
        self.mean = torch.tensor([0.485 * 255, 0.456 * 255, 0.406 * 255]).view(1,3,1,1)
        self.std = torch.tensor([0.229 * 255, 0.224 * 255, 0.225 * 255]).view(1,3,1,1)

        self.preload()

    def preload(self):
        try:
            self.next_input, self.next_target = next(self.loader)
        except StopIteration:
            self.next_input = None
            self.next_target = None
            return
        self.next_input = self.next_input.cpu()
        self.next_target = self.next_target.cpu()
        self.next_input = self.next_input.float()
        self.next_input = self.next_input.sub_(self.mean).div_(self.std)

    def next(self):
        input = self.next_input
        target = self.next_target
        self.preload()
        return input, target

def train(train_loader, model, criterion, optimizer, emulator, epoch, lr_scheduler):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to train mode
    model.train()
    end = time.time()

    prefetcher = data_prefetcher(train_loader)
    input, target = prefetcher.next()
    i = 0
    while input is not None:
        i += 1
        #adjust_learning_rate(optimizer, epoch, i, len(train_loader))
        lr_scheduler.update(i-1, epoch)

        # compute output
        output = model(input)
        loss = criterion(output, target)
        # compute gradient and do SGD step
        optimizer.zero_grad()

        with amp.scale_loss(loss, optimizer) as scaled_loss:
            scaled_loss.backward()

        if args.fp8_training:
            emulator.optimizer_step(optimizer)
        else :
            optimizer.step()

        if i%args.print_freq == 0:
            # Every print_freq iterations, check the loss, accuracy, and speed.
            # For best performance, it doesn't make sense to print these metrics every
            # iteration, since they incur an allreduce and some host<->device syncs.

            # Measure accuracy
            prec1, prec5 = accuracy(output.data, target, topk=(1, 5))

            # Average loss and accuracy across processes for logging
            if args.distributed:
                reduced_loss = reduce_tensor(loss.data)
                prec1 = reduce_tensor(prec1)
                prec5 = reduce_tensor(prec5)
            else:
                reduced_loss = loss.data

            # to_python_float incurs a host<->device sync
            losses.update(to_python_float(reduced_loss), input.size(0))
            top1.update(to_python_float(prec1), input.size(0))
            top5.update(to_python_float(prec5), input.size(0))

            batch_time.update((time.time() - end)/args.print_freq)
            end = time.time()

            if args.local_rank == 0:
                print('Epoch: [{0}][{1}/{2}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Speed {3:.3f} ({4:.3f})\t'
                      'lr {lr.learning_rate:.10f}\t'
                      'Loss {loss.val:.10f} ({loss.avg:.4f})\t'
                      'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                      'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                       epoch, i, len(train_loader),
                       args.world_size*args.batch_size/batch_time.val,
                       args.world_size*args.batch_size/batch_time.avg,
                       batch_time=batch_time,
                       lr=lr_scheduler,
                       loss=losses, top1=top1, top5=top5))
        input, target = prefetcher.next()


def validate(val_loader, model, criterion):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to evaluate mode
    model.eval()

    end = time.time()

    prefetcher = data_prefetcher(val_loader)
    input, target = prefetcher.next()
    i = 0
    while input is not None:
        i += 1

        # compute output
        with torch.no_grad():
            output = model(input)
            loss = criterion(output, target)

        # measure accuracy and record loss
        prec1, prec5 = accuracy(output.data, target, topk=(1, 5))

        if args.distributed:
            reduced_loss = reduce_tensor(loss.data)
            prec1 = reduce_tensor(prec1)
            prec5 = reduce_tensor(prec5)
        else:
            reduced_loss = loss.data

        losses.update(to_python_float(reduced_loss), input.size(0))
        top1.update(to_python_float(prec1), input.size(0))
        top5.update(to_python_float(prec5), input.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        # TODO:  Change timings to mirror train().
        if args.local_rank == 0 and i % args.print_freq == 0:
            print('Test: [{0}/{1}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Speed {2:.3f} ({3:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                  'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                   i, len(val_loader),
                   args.world_size * args.batch_size / batch_time.val,
                   args.world_size * args.batch_size / batch_time.avg,
                   batch_time=batch_time, loss=losses,
                   top1=top1, top5=top5))

        input, target = prefetcher.next()

    print(' * Prec@1 {top1.avg:.3f} Prec@5 {top5.avg:.3f}'
          .format(top1=top1, top5=top5))

    return top1.avg

def save_checkpoint(state, is_best, output_dir=".", filename='checkpoint.pth.tar'):
    cp_path      = os.path.join(output_dir,filename)
    best_cp_path = os.path.join(output_dir,'model_best.pth.tar')
    torch.save(state, cp_path)
    if is_best:
        shutil.copyfile(cp_path, best_cp_path)

class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def adjust_learning_rate(optimizer, epoch, step, len_epoch):
    """LR schedule that should yield 76% converged accuracy with batch size 256"""
    factor = epoch // 30

    if epoch >= 80:
        factor = factor + 1

    lr = args.lr*(0.1**factor)

    """Warmup"""
    if epoch < 5:
        lr = lr*float(1 + step + epoch*len_epoch)/(5.*len_epoch)

    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


def reduce_tensor(tensor):
    rt = tensor.clone()
    dist.all_reduce(rt, op=dist.ReduceOp.SUM)
    rt /= args.world_size
    return rt

if __name__ == '__main__':
    main()
