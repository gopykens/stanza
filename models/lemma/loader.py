import random
import numpy as np
import os
from collections import Counter
import torch

import models.common.seq2seq_constant as constant
from models.common.data import map_to_ids, get_long_tensor, get_float_tensor, sort_all
from models.common import conll
from models.lemma.vocab import Vocab
from models.lemma import edit

class DataLoader:
    def __init__(self, filename, batch_size, args, evaluation=False, conll_only=False):
        self.batch_size = batch_size
        self.args = args
        self.eval = evaluation
        self.shuffled = not self.eval

        assert filename.endswith('conllu'), "Loaded file must be conllu file."
        self.conll, data = self.load_file(filename)

        if conll_only: # only load conll file
            return

        # handle vocab
        vocab_file = "{}/{}.vocab".format(args['data_dir'], args['lang'])
        pos_vocab_file = "{}/{}.pos.vocab".format(args['data_dir'], args['lang'])
        self.vocab, self.pos_vocab = self.init_vocab(vocab_file, pos_vocab_file, data)

	# filter and sample data
        if args.get('sample_train', 1.0) < 1.0 and not self.eval:
            keep = int(args['sample_train'] * len(data))
            data = random.sample(data, keep)
            print("Subsample training set with rate {:g}".format(args['sample_train']))

        data = self.preprocess(data, self.vocab, self.pos_vocab, args)
        # shuffle for training
        if self.shuffled:
            indices = list(range(len(data)))
            random.shuffle(indices)
            data = [data[i] for i in indices]
        self.num_examples = len(data)

	# chunk into batches
        data = [data[i:i+batch_size] for i in range(0, len(data), batch_size)]
        self.data = data
        print("{} batches created for {}.".format(len(data), filename))

    def init_vocab(self, vocab_file, pos_vocab_file, data):
        if os.path.exists(vocab_file) and os.path.exists(pos_vocab_file):
            vocab = Vocab(vocab_file, load=True)
            pos_vocab = Vocab(pos_vocab_file, load=True)
        else:
            assert self.eval == False, "Vocab file must exist for evaluation"
            chars = "".join([d[0] + d[2] for d in data])
            char_counter = Counter(chars)
            vocab = Vocab(vocab_file, load=False, unit_counter=char_counter)
            poss = [d[1] for d in data]
            poss_counter = Counter(poss)
            pos_vocab = Vocab(pos_vocab_file, load=False, unit_counter=poss_counter)
        return vocab, pos_vocab

    def preprocess(self, data, vocab, pos_vocab, args):
        processed = []
        for d in data:
            edit_type = edit.EDIT_TO_ID[edit.get_edit_type(d[0], d[2])]
            src = list(d[0])
            src = [constant.SOS] + src + [constant.EOS]
            src = map_to_ids(src, vocab.unit2id)
            pos = d[1]
            pos = pos_vocab.unit2id[pos] if pos in pos_vocab.unit2id else constant.UNK_ID
            tgt = list(d[2])
            tgt_in = map_to_ids([constant.SOS] + tgt, vocab.unit2id)
            tgt_out = map_to_ids(tgt + [constant.EOS], vocab.unit2id)
            processed += [[src, tgt_in, tgt_out, pos, edit_type]]
        return processed

    def __len__(self):
        return len(self.data)

    def __getitem__(self, key):
        """ Get a batch with index. """
        if not isinstance(key, int):
            raise TypeError
        if key < 0 or key >= len(self.data):
            raise IndexError
        batch = self.data[key]
        batch_size = len(batch)
        batch = list(zip(*batch))
        assert len(batch) == 5

        # sort all fields by lens for easy RNN operations
        lens = [len(x) for x in batch[0]]
        batch, orig_idx = sort_all(batch, lens)

        # convert to tensors
        src = batch[0]
        src = get_long_tensor(src, batch_size)
        src_mask = torch.eq(src, constant.PAD_ID)
        tgt_in = get_long_tensor(batch[1], batch_size)
        tgt_out = get_long_tensor(batch[2], batch_size)
        pos = torch.LongTensor(batch[3])
        edits = torch.LongTensor(batch[4])
        assert tgt_in.size(1) == tgt_out.size(1), \
                "Target input and output sequence sizes do not match."
        return (src, src_mask, tgt_in, tgt_out, pos, edits, orig_idx)

    def __iter__(self):
        for i in range(self.__len__()):
            yield self.__getitem__(i)

    def load_file(self, filename):
        conll_file = conll.CoNLLFile(filename)
        data = conll_file.get(['word', 'upos', 'lemma'])
        return conll_file, data
