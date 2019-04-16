"""
Processor for performing lemmatization
"""

from stanfordnlp.models.common.conll import FIELD_TO_IDX
from stanfordnlp.models.lemma.data import DataLoader
from stanfordnlp.models.lemma.trainer import Trainer
from stanfordnlp.pipeline._constants import *
from stanfordnlp.pipeline.processor import UDProcessor


class LemmaProcessor(UDProcessor):

    # set of processor requirements this processor fulfills
    PROVIDES_DEFAULT = set([LEMMA])
    # set of processor requirements for this processor
    REQUIRES_DEFAULT = set([TOKENIZE])

    def __init__(self, config, pipeline, use_gpu):
        # run lemmatizer in identity mode
        self._use_identity = None
        super().__init__(config, pipeline, use_gpu)

    def _set_up_model(self, config, use_gpu):
        if config.get('use_identity') in ['True', True]:
            self._use_identity = True
            self._config = config
        else:
            self._use_identity = False
            self._trainer = Trainer(model_file=config['model_path'], use_cuda=use_gpu)

    def _set_up_requires(self):
        if self._config.get('pos') and not self._config.get('use_identity'):
            self._requires = LemmaProcessor.REQUIRES_DEFAULT.union(set([POS]))
        else:
            self._requires = LemmaProcessor.REQUIRES_DEFAULT

    def process(self, doc):
        if not self._use_identity:
            batch = DataLoader(doc, self._config['batch_size'], self._config, vocab=self._vocab, evaluation=True)
        else:
            batch = DataLoader(doc, self._config['batch_size'], self._config, evaluation=True, conll_only=True)
        if self._use_identity:
            preds = [ln[FIELD_TO_IDX['word']] for sent in batch.conll.sents for ln in sent if '-' not in ln[0]]
        elif self._config.get('dict_only', False):
            preds = self._trainer.predict_dict(batch.conll.get(['word', 'upos']))
        else:
            if self._config.get('ensemble_dict', False):
                # skip the seq2seq model when we can
                skip = self.trainer.skip_seq2seq(batch.conll.get(['word', 'upos']))
                seq2seq_batch = DataLoader(doc, self._config['batch_size'], self._config, vocab=self.vocab, evaluation=True, skip=skip)
            else:
                seq2seq_batch = batch

            preds = []
            edits = []
            for i, b in enumerate(seq2seq_batch):
                ps, es = self._trainer.predict(b, self._config['beam_size'])
                preds += ps
                if es is not None:
                    edits += es

            if self._config.get('ensemble_dict', False):
                preds = self._trainer.postprocess([x for x, y in zip(batch.conll.get(['word']), skip) if not y], preds, edits=edits)
                # expand seq2seq predictions to the same size as all words
                i = 0
                preds1 = []
                for s in skip:
                    if s:
                        preds1.append('')
                    else:
                        preds1.append(preds[i])
                        i += 1
                preds = self._trainer.ensemble(batch.conll.get(['word', 'upos']), preds1)
            else:
                preds = self._trainer.postprocess(batch.conll.get(['word']), preds, edits=edits)

        # map empty string lemmas to '_'
        preds = [max([(len(x), x), (0, '_')])[1] for x in preds]
        batch.conll.set(['lemma'], preds)

