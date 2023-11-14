#------------------------------------------------------------------------------ 
# Copyright (c) 2023, Intel Corporation - All rights reserved. 
# This file is part of FP8-Emulation-Toolkit
#
# SPDX-License-Identifier: BSD-3-Clause
#------------------------------------------------------------------------------
# Naveen Mellempudi (Intel Corporation)
#------------------------------------------------------------------------------

import torch
from mpemu.cmodel import simple

def get_grads(variables):
    return [var.grad.clone() for var in variables]

m=512 
n=256
k=1024

a = torch.rand((m, k), dtype=torch.float32)
b = torch.rand((n, k), dtype=torch.float32)
c = torch.zeros((m, n), dtype=torch.float32)


a64 = a.to(dtype=torch.float64, copy=True)
b64 = b.to(dtype=torch.float64, copy=True)
c64 = c.to(dtype=torch.float64, copy=True)

z = torch.matmul(a64, b64.t())
z2 = simple.matmul(a, b.t())
z3 = torch.matmul(a, b.t())

print('32b: L2 distance : ', torch.dist(z, z3.to(dtype=torch.float64, copy=True), 2))
print('output : L2 distance : ', torch.dist(z, z2.to(dtype=torch.float64, copy=True), 2))
