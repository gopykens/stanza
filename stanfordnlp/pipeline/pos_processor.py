"""
Processor for performing part-of-speech tagging
"""

from stanfordnlp.models.common.pretrain import Pretrain
from stanfordnlp.models.pos.data import DataLoader
from stanfordnlp.models.pos.trainer import Trainer
from stanfordnlp.pipeline._constants import *
from stanfordnlp.pipeline.processor import UDProcessor


class POSProcessor(UDProcessor):

    # set of processor requirements this processor fulfills
    PROVIDES_DEFAULT = set([POS])
    # set of processor requirements for this processor
    REQUIRES_DEFAULT = set([TOKENIZE])

    def _set_up_model(self, config, use_gpu):
        # get pretrained word vectors
        self._pretrain = Pretrain(config['pretrain_path'])
        # set up trainer
        self._trainer = Trainer(pretrain=self._pretrain, model_file=config['model_path'], use_cuda=use_gpu)

    def process(self, doc):
        batch = DataLoader(
            doc, self._config['batch_size'], self._config, self._pretrain, vocab=self._vocab, evaluation=True)
        preds = []
        for i, b in enumerate(batch):
            preds += self._trainer.predict(b)
        batch.conll.set(['upos', 'xpos', 'feats'], [y for x in preds for y in x])


