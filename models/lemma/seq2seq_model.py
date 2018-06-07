"""
The full container encoder-decoder model, built on top of
base seq2seq modules.
"""

import torch
from torch import nn
from torch.autograd import Variable
import torch.nn.functional as F
import numpy as np

from models.lemma import constant, utils
from models.lemma.modules import LSTMAttention
from models.lemma.beam import Beam

class Seq2SeqModel(nn.Module):
    """
    A complete encoder-decoder model, with optional attention.
    """
    def __init__(self, args, emb_matrix=None):
        super().__init__()
        self.vocab_size = args['vocab_size']
        self.emb_dim = args['emb_dim']
        self.hidden_dim = args['hidden_dim']
        self.nlayers = args['num_layers'] # encoder layers, decoder layers = 1
        self.emb_dropout = args.get('emb_dropout', 0.0)
        self.dropout = args['dropout']
        self.pad_token = constant.PAD_ID
        self.max_dec_len = args['max_dec_len']
        self.use_cuda = args['cuda']
        self.top = args.get('top', 1e10)
        self.args = args
        self.emb_matrix = emb_matrix
        
        print("Building an attentional Seq2Seq model...")
        print("Using a Bi-LSTM encoder")
        self.num_directions = 2
        self.enc_hidden_dim = self.hidden_dim // 2
        self.dec_hidden_dim = self.hidden_dim
        
        self.emb_drop = nn.Dropout(self.emb_dropout)
        self.drop = nn.Dropout(self.dropout)
        self.embedding = nn.Embedding(self.vocab_size, self.emb_dim, self.pad_token)
        self.encoder = nn.LSTM(self.emb_dim, self.enc_hidden_dim, self.nlayers, \
                bidirectional=True, batch_first=True, dropout=self.dropout)
        self.decoder = LSTMAttention(self.emb_dim, self.dec_hidden_dim, \
                batch_first=True)
        self.dec2vocab = nn.Linear(self.dec_hidden_dim, self.vocab_size)
        
        self.SOS_tensor = torch.LongTensor([constant.SOS_ID])
        self.SOS_tensor = self.SOS_tensor.cuda() if self.use_cuda else self.SOS_tensor

        self.init_weights()
            
    def init_weights(self):
        # initialize embeddings
        if self.emb_matrix is not None:
            if isinstance(self.emb_matrix, np.ndarray):
                self.emb_matrix = torch.from_numpy(self.emb_matrix)
            assert self.emb_matrix.size() == (self.vocab_size, self.emb_dim), \
                    "Input embedding matrix must match size: {} x {}".format(self.vocab_size, self.emb_dim)
            self.embedding.weight.data.copy_(self.emb_matrix)
        else:
            init_range = constant.EMB_INIT_RANGE
            self.embedding.weight.data.uniform_(-init_range, init_range)
        # decide finetuning
        if self.top <= 0:
            print("Do not finetune embedding layer.")
            self.embedding.weight.requires_grad = False
        elif self.top < self.vocab_size:
            print("Finetune top {} embeddings.".format(self.top))
            self.embedding.weight.register_hook(lambda x: utils.keep_partial_grad(x, self.top))
        else:
            print("Finetune all embeddings.")
        #self.dec2vocab.bias.data.fill_(0.0)

    def zero_state(self, inputs):
        batch_size = inputs.size(0)
        h0 = Variable(torch.zeros(self.encoder.num_layers*2, batch_size, self.enc_hidden_dim), requires_grad=False)
        c0 = Variable(torch.zeros(self.encoder.num_layers*2, batch_size, self.enc_hidden_dim), requires_grad=False)
        if self.use_cuda:
            return h0.cuda(), c0.cuda()
        return h0, c0
    
    def encode(self, enc_inputs, lens):
        """ Encode source sequence. """
        self.h0, self.c0 = self.zero_state(enc_inputs)

        packed_inputs = nn.utils.rnn.pack_padded_sequence(enc_inputs, lens, batch_first=True)
        packed_h_in, (hn, cn) = self.encoder(packed_inputs, (self.h0, self.c0))
        h_in, _ = nn.utils.rnn.pad_packed_sequence(packed_h_in, batch_first=True)
        hn = torch.cat((hn[-1], hn[-2]), 1)
        cn = torch.cat((cn[-1], cn[-2]), 1)
        return h_in, (hn, cn)

    def decode(self, dec_inputs, hn, cn, ctx, ctx_mask=None):
        """ Decode a step, based on context encoding and source context states."""
        dec_hidden = (hn, cn)
        h_out, dec_hidden = self.decoder(dec_inputs, dec_hidden, ctx, ctx_mask)
        
        h_out_reshape = h_out.contiguous().view(h_out.size(0) * h_out.size(1), -1)
        decoder_logits = self.dec2vocab(h_out_reshape)
        decoder_logits = decoder_logits.view(h_out.size(0), h_out.size(1), -1)
        log_probs = self.get_log_prob(decoder_logits)
        return log_probs, dec_hidden

    def forward(self, src, src_mask, tgt_in):
        # prepare for encoder/decoder
        enc_inputs = self.emb_drop(self.embedding(src))
        dec_inputs = self.emb_drop(self.embedding(tgt_in))
        src_lens = list(src_mask.data.eq(constant.PAD_ID).long().sum(1).squeeze())
        
        h_in, (hn, cn) = self.encode(enc_inputs, src_lens)
        log_probs, _ = self.decode(dec_inputs, hn, cn, h_in, src_mask) 
        return log_probs

    def get_log_prob(self, logits):
        logits_reshape = logits.view(-1, self.vocab_size)
        log_probs = F.log_softmax(logits_reshape, dim=1)
        if logits.dim() == 2:
            return log_probs
        return log_probs.view(logits.size(0), logits.size(1), logits.size(2))

    def predict_greedy(self, src, src_mask, beam_size=1):
        """ Predict with greedy decoding. """
        enc_inputs = self.embedding(src)
        batch_size = enc_inputs.size(0)
        src_lens = list(src_mask.data.eq(constant.PAD_ID).long().sum(1).squeeze())
        
        # encode source
        h_in, (hn, cn) = self.encode(enc_inputs, src_lens)
        
        # greedy decode by step
        dec_inputs = self.embedding(Variable(self.SOS_tensor))
        dec_inputs = dec_inputs.expand(batch_size, dec_inputs.size(0), dec_inputs.size(1))

        done = [False for _ in range(batch_size)]
        total_done = 0
        max_len = 0
        output_seqs = [[] for _ in range(batch_size)]

        while total_done < batch_size and max_len < self.max_dec_len:
            log_probs, (hn, cn) = self.decode(dec_inputs, hn, cn, h_in, src_mask)
            assert log_probs.size(1) == 1, "Output must have 1-step of output."
            _, preds = log_probs.squeeze(1).max(1, keepdim=True)
            dec_inputs = self.embedding(preds) # update decoder inputs
            max_len += 1
            for i in range(batch_size):
                if not done[i]:
                    token = preds.data[i][0]
                    if token == constant.EOS_ID:
                        done[i] == True
                        total_done += 1
                    else:
                        output_seqs[i].append(token)
        return output_seqs

    def predict(self, src, src_mask, beam_size=5):
        """ Predict with beam search. """
        enc_inputs = self.embedding(src)
        batch_size = enc_inputs.size(0)
        src_lens = list(src_mask.data.eq(constant.PAD_ID).long().sum(1).squeeze())
        
        # (1) encode source
        h_in, (hn, cn) = self.encode(enc_inputs, src_lens)

        # (2) set up beam
        h_in = Variable(h_in.data.repeat(beam_size, 1, 1), volatile=True) # repeat data for beam search
        src_mask = src_mask.repeat(beam_size, 1)
        # repeat decoder hidden states 
        hn = Variable(hn.data.repeat(beam_size, 1), volatile=True)
        cn = Variable(cn.data.repeat(beam_size, 1), volatile=True)
        beam = [Beam(beam_size, self.use_cuda) for _ in range(batch_size)]

        def update_state(states, idx, positions, beam_size):
            """ Select the states according to back pointers. """
            for e in states:
                br, d = e.size()
                s = e.contiguous().view(beam_size, br // beam_size, d)[:,idx]
                s.data.copy_(s.data.index_select(0, positions))

        # (3) main loop
        for i in range(self.max_dec_len):
            dec_inputs = torch.stack([b.get_current_state() for b in beam]).t().contiguous().view(-1, 1)
            dec_inputs = self.embedding(Variable(dec_inputs))
            log_probs, (hn, cn) = self.decode(dec_inputs, hn, cn, h_in, src_mask)
            log_probs = log_probs.view(beam_size, batch_size, -1).transpose(0,1)\
                    .contiguous() # [batch, beam, V]

            # advance each beam
            done = []
            for b in range(batch_size):
                is_done = beam[b].advance(log_probs.data[b])
                if is_done:
                    done += [b]
                # update beam state
                update_state((hn, cn), b, beam[b].get_current_origin(), beam_size)

            if len(done) == batch_size:
                break

        # back trace and find hypothesis
        all_hyp, all_scores = [], []
        for b in range(batch_size):
            scores, ks = beam[b].sort_best()
            all_scores += [scores[0]]
            k = ks[0]
            hyp = beam[b].get_hyp(k)
            hyp = utils.prune_hyp(hyp)
            all_hyp += [hyp]

        return all_hyp


