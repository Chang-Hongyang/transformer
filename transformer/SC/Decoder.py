#encoding: utf-8

import torch
from torch import nn

from modules.base import SelfAttn, CrossAttn, PositionalEmb, Dropout, ResidueCombiner
from modules.TA import PositionwiseFF

from utils import repeat_bsize_for_beam_tensor
from math import sqrt

from transformer.Decoder import Decoder as DecoderBase

class DecoderLayer(nn.Module):

	def __init__(self, isize, fhsize=None, dropout=0.0, attn_drop=0.0, num_head=8, ahsize=None):

		super(DecoderLayer, self).__init__()

		_ahsize = isize if ahsize is None else ahsize

		_fhsize = _ahsize * 4 if fhsize is None else fhsize

		self.self_attn = SelfAttn(isize, _ahsize, isize, num_head, dropout=attn_drop)
		self.cross_attn = CrossAttn(isize, _ahsize, isize, num_head, dropout=attn_drop)

		self.ff = PositionwiseFF(isize, _fhsize, dropout)
		self.scff = ResidueCombiner(isize, 2, _fhsize)

		self.layer_normer1 = nn.LayerNorm(isize, eps=1e-06)
		self.layer_normer2 = nn.LayerNorm(isize, eps=1e-06)

		if dropout > 0:
			self.d1 = Dropout(dropout, inplace=True)
			self.d2 = Dropout(dropout, inplace=True)
		else:
			self.d1 = None
			self.d2 = None

	def forward(self, inpute, inputh, inputo, src_pad_mask=None, tgt_pad_mask=None, query_unit=None, concat_query=False):

		if query_unit is None:

			states_return = None

			_inputo = self.scff(inputo, inputh)

			context = self.self_attn(_inputo, mask=tgt_pad_mask)

			if self.d1 is not None:
				context = self.d1(context)

			context = context + _inputo

		else:

			_query_unit = self.scff(query_unit, inputh)

			if concat_query:

				inputo = _query_unit if inputo is None else torch.cat((inputo, _query_unit,), 1)

			states_return = inputo

			context = self.self_attn(_query_unit, iK=inputo)

			if self.d1 is not None:
				context = self.d1(context)

			context = context + _query_unit

		_context = self.layer_normer1(context)

		_context_new = self.cross_attn(_context, inpute, mask=src_pad_mask)

		if self.d2 is not None:
			_context_new = self.d2(_context_new)

		context = self.layer_normer2(_context_new + _context)

		context = self.ff(context)

		if states_return is None:
			return context
		else:
			return context, states_return

class Decoder(DecoderBase):

	def __init__(self, isize, nwd, num_layer, fhsize=None, dropout=0.0, attn_drop=0.0, emb_w=None, num_head=8, xseql=512, ahsize=None, norm_output=True, bindemb=False, forbidden_index=None):

		_ahsize = isize if ahsize is None else ahsize

		_fhsize = _ahsize * 4 if fhsize is None else fhsize

		super(Decoder, self).__init__(isize, nwd, num_layer, _fhsize, dropout, attn_drop, emb_w, num_head, xseql, _ahsize, norm_output, bindemb, forbidden_index)

		self.nets = nn.ModuleList([DecoderLayer(isize, _fhsize, dropout, attn_drop, num_head, _ahsize) for i in range(num_layer)])

	def forward(self, inpute, inputh, inputo, src_pad_mask=None):

		bsize, nquery = inputo.size()

		out = self.wemb(inputo)

		out = out * sqrt(out.size(-1)) + self.pemb(inputo, expand=False)

		if self.drop is not None:
			out = self.drop(out)

		out = self.out_normer(out)

		_mask = self._get_subsequent_mask(nquery)

		for net, inputu, inputhu in zip(self.nets, inpute.unbind(dim=-1), inputh.unbind(dim=-1)):
			out = net(inputu, inputhu, out, src_pad_mask, _mask)

		out = self.lsm(self.classifier(out))

		return out

	def greedy_decode(self, inpute, inputh, src_pad_mask=None, max_len=512):

		bsize, seql= inpute.size()[:2]

		sos_emb = self.get_sos_emb(inpute)

		sqrt_isize = sqrt(sos_emb.size(-1))

		out = sos_emb * sqrt_isize + self.pemb.get_pos(0).view(1, 1, -1).expand(bsize, 1, -1)

		if self.drop is not None:
			out = self.drop(out)

		out = self.out_normer(out)

		states = {}

		for _tmp, (net, inputu, inputhu) in enumerate(zip(self.nets, inpute.unbind(dim=-1), inputh.unbind(dim=-1))):
			out, _state = net(inputu, inputhu, None, src_pad_mask, None, out, True)
			states[_tmp] = _state

		out = self.lsm(self.classifier(out))

		wds = out.argmax(dim=-1)

		trans = [wds]

		done_trans = wds.squeeze(1).eq(2)

		for i in range(1, max_len):

			out = self.wemb(wds) * sqrt_isize + self.pemb.get_pos(i).view(1, 1, -1).expand(bsize, 1, -1)

			if self.drop is not None:
				out = self.drop(out)

			out = self.out_normer(out)

			for _tmp, (net, inputu, inputhu) in enumerate(zip(self.nets, inpute.unbind(dim=-1), inputh.unbind(dim=-1))):
				out, _state = net(inputu, inputhu, states[_tmp], src_pad_mask, None, out, True)
				states[_tmp] = _state

			out = self.lsm(self.classifier(out))
			wds = out.argmax(dim=-1)

			trans.append(wds)

			done_trans = (done_trans + wds.squeeze(1).eq(2)).gt(0)
			if done_trans.sum().item() == bsize:
				break

		return torch.cat(trans, 1)

	def beam_decode(self, inpute, inputh, src_pad_mask=None, beam_size=8, max_len=512, length_penalty=0.0, return_all=False, clip_beam=False):

		bsize, seql = inpute.size()[:2]

		beam_size2 = beam_size * beam_size
		bsizeb2 = bsize * beam_size2
		real_bsize = bsize * beam_size

		sos_emb = self.get_sos_emb(inpute)
		isize = sos_emb.size(-1)
		sqrt_isize = sqrt(isize)

		if length_penalty > 0.0:
			lpv = sos_emb.new_ones(real_bsize, 1)
			lpv_base = 6.0 ** length_penalty

		out = sos_emb * sqrt_isize + self.pemb.get_pos(0).view(1, 1, isize).expand(bsize, 1, isize)

		if self.drop is not None:
			out = self.drop(out)

		out = self.out_normer(out)

		states = {}

		for _tmp, (net, inputu, inputhu) in enumerate(zip(self.nets, inpute.unbind(dim=-1), inputh.unbind(dim=-1))):
			out, _state = net(inputu, inputhu, None, src_pad_mask, None, out, True)
			states[_tmp] = _state

		out = self.lsm(self.classifier(out))

		scores, wds = out.topk(beam_size, dim=-1)
		scores = scores.squeeze(1)
		sum_scores = scores
		wds = wds.view(real_bsize, 1)
		trans = wds

		done_trans = wds.view(bsize, beam_size).eq(2)

		inpute = inpute.repeat(1, beam_size, 1, 1).view(real_bsize, seql, isize, -1)
		inputh = repeat_bsize_for_beam_tensor(inputh, beam_size)

		_src_pad_mask = None if src_pad_mask is None else src_pad_mask.repeat(1, beam_size, 1).view(real_bsize, 1, seql)

		for key, value in states.items():
			states[key] = repeat_bsize_for_beam_tensor(value, beam_size)

		for step in range(1, max_len):

			out = self.wemb(wds) * sqrt_isize + self.pemb.get_pos(step).view(1, 1, isize).expand(real_bsize, 1, isize)

			if self.drop is not None:
				out = self.drop(out)

			out = self.out_normer(out)

			for _tmp, (net, inputu, inputhu) in enumerate(zip(self.nets, inpute.unbind(dim=-1), inputh.unbind(dim=-1))):
				out, _state = net(inputu, inputhu, states[_tmp], _src_pad_mask, None, out, True)
				states[_tmp] = _state

			out = self.lsm(self.classifier(out)).view(bsize, beam_size, -1)

			_scores, _wds = out.topk(beam_size, dim=-1)
			_scores = (_scores.masked_fill(done_trans.unsqueeze(2).expand(bsize, beam_size, beam_size), 0.0) + sum_scores.unsqueeze(2).expand(bsize, beam_size, beam_size))

			if length_penalty > 0.0:
				lpv = lpv.masked_fill(1 - done_trans.view(real_bsize, 1), ((step + 6.0) ** length_penalty) / lpv_base)

			if clip_beam and (length_penalty > 0.0):
				scores, _inds = (_scores.view(real_bsize, beam_size) / lpv.expand(real_bsize, beam_size)).view(bsize, beam_size2).topk(beam_size, dim=-1)
				_tinds = (_inds + torch.arange(0, bsizeb2, beam_size2, dtype=_inds.dtype, device=_inds.device).unsqueeze(1).expand_as(_inds)).view(real_bsize)
				sum_scores = _scores.view(bsizeb2).index_select(0, _tinds).view(bsize, beam_size)
			else:
				scores, _inds = _scores.view(bsize, beam_size2).topk(beam_size, dim=-1)
				_tinds = (_inds + torch.arange(0, bsizeb2, beam_size2, dtype=_inds.dtype, device=_inds.device).unsqueeze(1).expand_as(_inds)).view(real_bsize)
				sum_scores = scores

			wds = _wds.view(bsizeb2).index_select(0, _tinds).view(real_bsize, 1)

			_inds = (_inds / beam_size + torch.arange(0, real_bsize, beam_size, dtype=_inds.dtype, device=_inds.device).unsqueeze(1).expand_as(_inds)).view(real_bsize)

			trans = torch.cat((trans.index_select(0, _inds), wds), 1)

			done_trans = (done_trans.view(real_bsize).index_select(0, _inds) + wds.eq(2).squeeze(1)).gt(0).view(bsize, beam_size)

			_done = False
			if length_penalty > 0.0:
				lpv = lpv.index_select(0, _inds)	
			elif (not return_all) and done_trans.select(1, 0).sum().item() == bsize:
				_done = True

			if _done or (done_trans.sum().item() == real_bsize):
				break

			for key, value in states.items():
				states[key] = value.index_select(0, _inds)

		if (not clip_beam) and (length_penalty > 0.0):
			scores = scores / lpv.view(bsize, beam_size)
			scores, _inds = scores.topk(beam_size, dim=-1)
			_inds = (_inds + torch.arange(0, real_bsize, beam_size, dtype=_inds.dtype, device=_inds.device).unsqueeze(1).expand_as(_inds)).view(real_bsize)
			trans = trans.view(real_bsize, -1).index_select(0, _inds).view(bsize, beam_size, -1)

		if return_all:

			return trans, scores
		else:

			return trans.view(bsize, beam_size, -1).select(1, 0)

	def decode(self, inpute, inputh, src_pad_mask, beam_size=1, max_len=512, length_penalty=0.0):

		return self.beam_decode(inpute, inputh, src_pad_mask, beam_size, max_len, length_penalty) if beam_size > 1 else self.greedy_decode(inpute, inputh, src_pad_mask, max_len)