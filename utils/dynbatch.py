#encoding: utf-8

import torch

from math import log2, exp, pi, acos
from random import random

# comment the following line and uncomment the 4 lines following it to load para_group_select_alpha from cnfg.dynb
para_group_select_alpha = 3.0
'''try:
	from cnfg.dynb import select_alpha as para_group_select_alpha
except Exception:
	para_group_select_alpha = 3.0'''

def gumble_random():

	return -log2(-log2(random()))

def add_gumble(lin):

	return [lu - log2(-log2(random())) for lu in lin]

def softmax(lin):

	_mv = max(*lin)
	_tmp = [exp(tmpu - _mv) for tmpu in lin]
	_mv = sum(_tmp)

	return [tmpu / _mv for tmpu in _tmp]

def pos_norm(lin, alpha=1.0):

	tmp = [0.0 if tmpu < 0.0 else tmpu for tmpu in lin]
	if alpha != 1.0:
		tmp = [tmpu ** alpha for tmpu in tmp]
	_mv = sum(tmp)

	return [tmpu / _mv for tmpu in tmp]

def backup_para_grad(plin):

	with torch.no_grad():
		rs = [pu.grad.clone() for pu in plin]

	return rs

def prep_cos(og, ng):

	return (og * ng).sum(), og.pow(2).sum(), ng.pow(2).sum()

def cos_acc_pg(old_pg, new_pg):

	with torch.no_grad():
		on, o, n = zip(*[prep_cos(ou, nu) for ou, nu in zip(old_pg, new_pg)])
		sim = (torch.stack(on, 0).sum() / (torch.stack(o, 0).sum() * torch.stack(n, 0).sum()).sqrt()).item()

	return sim

class EffRecoder:

	def __init__(self, num_choice, num_his=50, init_value=180.0):

		self.his = [[init_value] for i in range(num_choice)]
		self.num_his = num_his

	def update_eff(self, ind, value):

		_lsel = self.his[ind]
		_lsel.append(value)
		_lind = len(_lsel) - self.num_his
		if _lind > 0:
			self.his[ind] = _lsel[_lind:]

	def get_w(self):

		return [sum(tmpu) / len(tmpu) for tmpu in self.his]

class GradientMonitor:

	# num_group: number of parameter groups
	# select_func: a function takes (model, index) as input arguments, which returns the index parameter group of model.
	# angle_alpha: the alpha value, if the angle change is greater than or equal to the multiplication of the minimum value in the history and the alpha, this class will return True to require performing an optimization step.
	# num_tol_amin: number of tolerant steps after the minimum angle change, if fails to obtain a smaller angle change after this number of steps, will return True to ask performing an optimization step.
	# num_his_recoder: number of records of the angle change reduction.
	# num_his_gm: cache num_his_gm gradients into a history, and return this number of angle changes.
	# returns: (update_r, angle_r), update_r: to performing an optimization step, angle_r: the angle change in current step.

	def __init__(self, num_group, select_func, angle_alpha=1.1, num_tol_amin=3, num_his_recoder=50, num_his_gm=1):

		self.scale = 180.0 / pi
		self.num_group = num_group
		self.recorder = EffRecoder(num_group, num_his=num_his_recoder, init_value=1.0)#init_value=180.0 if use sample_gumble_norm in self.reset
		self.select_func = select_func
		self.alpha, self.num_tol_amin, self.num_his = angle_alpha, num_tol_amin, num_his_gm
		self.reset()

	def update(self, mod):

		_cur_gg = backup_para_grad(self.select_func(mod, self.sel_ind))
		angle_r = None
		if self.num_his > 1:
			if self.prev_grad is None:
				self.prev_grad = [_cur_gg]
			else:
				angle_r = [(acos(cos_acc_pg(prev_gu, _cur_gg)) * self.scale) for prev_gu in reversed(self.prev_grad)]
				angle_r_c = angle_r[0]
				self.agree_his.append(angle_r_c)
				self.prev_grad.append(_cur_gg)
				_cur_np = len(self.prev_grad)
				if _cur_np >= self.num_his:
					self.prev_grad = self.prev_grad[_cur_np - self.num_his:]
		else:
			if self.prev_grad is None:
				self.prev_grad = _cur_gg
			else:
				angle_r = angle_r_c = acos(cos_acc_pg(self.prev_grad, _cur_gg)) * self.scale
				self.agree_his.append(angle_r_c)
				self.prev_grad = _cur_gg
		update_r = False
		if angle_r is not None:
			if self.min_ang is None:
				self.min_ang, self.max_ang = angle_r_c, angle_r_c * self.alpha
			else:
				if angle_r_c < self.min_ang:
					self.min_ang, self.max_ang = angle_r_c, angle_r_c * self.alpha
				elif angle_r_c >= self.max_ang:
					update_r = True
				else:
					self.namin += 1
					if self.namin >= self.num_tol_amin:
						update_r = True

		if update_r:
			self.recorder.update_eff(self.sel_ind, get_delta_norm(self.agree_his))
			self.reset()

		return update_r, angle_r

	def reset(self):

		self.prev_grad = None
		self.min_ang = None
		self.max_ang = None
		self.namin = 0
		self.agree_his = []
		self.sel_ind = sample_norm_softmax(self.recorder.get_w())#sample_gumble_norm(self.recorder.get_w())

def get_delta(lin):

	return max(*lin) - min(*lin)

def get_delta_norm(lin):

	_mv = max(*lin)

	return (_mv - min(*lin)) / _mv

def select_max(lin):

	rs_ind = 0
	max_v = max(*lin)
	for i, v in enumerate(lin):
		if max_v == v:
			rs_ind = i
			break

	return rs_ind

def select_gumble_max(lin):

	return select_max(add_gumble(lin))

def sample_norm_softmax(lin):

	_p = random()
	rs_ind = len(lin) - 1
	for i, lu in enumerate(softmax(lin)):
		_p -= lu
		if _p < 0.0:
			rs_ind = i
			break

	return rs_ind

def sample_norm(lin, alpha=1.0):

	_p = random()
	rs_ind = len(lin) - 1
	for i, lu in enumerate(pos_norm(lin, alpha)):
		_p -= lu
		if _p < 0.0:
			rs_ind = i
			break

	return rs_ind

def sample_gumble_norm(lin, alpha=para_group_select_alpha):

	return sample_norm(add_gumble(lin), alpha)
