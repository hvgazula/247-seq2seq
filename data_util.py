import glob
import math
import os
import sys
from collections import Counter
from multiprocessing import Pool

import numpy as np
import pandas as pd
import sentencepiece as spm
import torch
from scipy.io import loadmat
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import Dataset


# Read file line-by-line and store in list
def read_file(fn):
    with open(fn, 'r') as f:
        lines = [line.rstrip() for line in f]
    return lines


# Get electrode date helper
def get_electrode(elec_id):
    conversation, electrode = elec_id
    search_str = conversation + f'/preprocessed/*_{electrode}.mat'
    mat_fn = glob.glob(search_str)
    if len(mat_fn) == 0:
        print(f'[WARNING] electrode {electrode} DNE in {search_str}')
        return None
    return loadmat(mat_fn[0])['p1st'].squeeze().astype(np.float32)


def return_electrode_array(conv, elect):
    # Read signals
    elec_ids = ((conv, electrode) for electrode in elect)
    with Pool() as pool:
        ecogs = list(
            filter(lambda x: x is not None, pool.map(get_electrode, elec_ids)))

    ecogs = np.asarray(ecogs)
    ecogs = (ecogs - ecogs.mean(axis=1).reshape(
        ecogs.shape[0], 1)) / ecogs.std(axis=1).reshape(ecogs.shape[0], 1)
    ecogs = ecogs.T
    assert (ecogs.ndim == 2 and ecogs.shape[1] == len(elect))
    return ecogs


def return_examples(file, delim, vocabulary, ex_words):
    with open(file, 'r') as fin:
        lines = map(lambda x: x.split(delim), fin)
        examples = map(
            lambda x: (" ".join([
                z for y in x[0:-4]
                if (z:= y.lower().strip().replace('"', '')) not in ex_words
            ]), x[-1].strip() == "Speaker1", x[-4], x[-3]), lines)
        examples = filter(lambda x: len(x[0]) > 0, examples)
        examples = map(
            lambda x: (vocabulary.EncodeAsIds(x[0]), x[1], int(float(x[2])),
                       int(float(x[3]))), examples)
        return list(examples)


def generate_wordpairs(examples):
    '''if the first set already has two words and is speaker 1
        if the second set already has two words and is speaker 1
        the onset of the first word is earlier than the second word
    '''
    my_grams = []
    for first, second in zip(examples, examples[1:]):
        len1, len2 = len(first[0]), len(second[0])
        if first[1] and len1 == 2:
            my_grams.append(first)
        if second[1] and len2 == 2:
            my_grams.append(second)
        if ((first[1] and second[1]) and (len1 == 1 and len2 == 1)
                and (first[2] < second[2])):
            ak = (first[0] + second[0], True, first[2], second[3])
            my_grams.append(ak)
    return my_grams


def remove_duplicates(grams):
    df = pd.DataFrame(grams)
    df[['fw', 'sw']] = pd.DataFrame(df[0].tolist())
    df = df.drop(columns=[0]).drop_duplicates()
    df[0] = df[['fw', 'sw']].values.tolist()
    df = df.drop(columns=['fw', 'sw'])
    df = df[sorted(df.columns)]
    return list(df.to_records(index=False))


def remove_oovs(grams, vocabulary, data_tag='train'):
    if data_tag == 'train':
        grams = filter(lambda x: vocabulary['<unk>'] not in x[0], grams)
    else:
        grams = filter(lambda x: x[0] != [vocabulary['<unk>']] * 2, grams)
    return list(grams)


def add_begin_end_tokens(word_pair, vocabulary, start_tok, stop_tok):
    word_pair.insert(0, vocabulary[start_tok])  # Add start token
    word_pair.append(vocabulary[stop_tok])  # Add end token
    return word_pair


def test_for_bad_window(start, stop, shape, window):
    # if the window_begin is less than 0 or
    # check if onset is within limits
    # if the window_end is less than 0 or
    # if the window_end is outside the signal
    # if there are not enough frames in the window
    return (start < 0 or start > shape[0] or stop < 0 or stop > shape[0]
            or stop - start < window or stop - start < 0)


def calculate_windows_params(gram, param_dict):
    seq_length = gram[3] - gram[2]
    begin_window = gram[2] + param_dict['start_offset']
    end_window = gram[3] + param_dict['end_offset']
    bin_size = int(
        math.ceil((end_window - begin_window) /
                  param_dict['bin_fs']))  # calculate number of bins

    return seq_length, begin_window, end_window, bin_size


# Pytorch Dataset wrapper
class Brain2enDataset(Dataset):
    """Brainwave-to-English Dataset."""
    def __init__(self, signals, labels):
        """
        Args:
            signals (list): brainwave examples.
            labels (list): english examples.
        """
        # global oov_token, vocab

        assert (len(signals) == len(labels))
        indices = [(i, len(signals[i]), len(labels[i]))
                   for i in range(len(signals))]
        indices.sort(key=lambda x: (x[1], x[2], x[0]))
        self.examples = []
        self.max_seq_len = 0
        self.max_sent_len = 0
        self.train_freq = Counter()
        c = 0
        for i in indices:
            if i[1] > 384 or i[2] < 4 or i[2] > 128:
                c += 1
                continue
            lab = labels[i[0]]
            self.train_freq.update(lab)
            lab = torch.tensor(lab).long()
            self.examples.append(
                (torch.from_numpy(signals[i[0]]).float(), lab))
            self.max_seq_len = max(self.max_seq_len, i[1])
            self.max_sent_len = max(self.max_sent_len, len(lab))
        print("Skipped", c, "examples")

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        return self.examples[idx]


class MyCollator(object):
    def __init__(self, CONFIG, vocabulary):
        self.CONFIG = CONFIG
        self.vocabulary = vocabulary
        self.pad_token = CONFIG["pad_token"]

    def __call__(self, batch):
        # do something with batch and self.params
        src = pad_sequence([batch[i][0] for i in range(len(batch))],
                           batch_first=True,
                           padding_value=0.)
        labels = pad_sequence([batch[i][1] for i in range(len(batch))],
                              batch_first=True,
                              padding_value=self.vocabulary[self.pad_token])
        trg = torch.zeros(labels.size(0), labels.size(1),
                          len(self.vocabulary)).scatter_(
                              2, labels.unsqueeze(-1), 1)
        trg, trg_y = trg[:, :-1, :], labels[:, 1:]
        pos_mask, pad_mask = self.masks(trg_y)
        return src, trg, trg_y, pos_mask, pad_mask

    def masks(self, labels):
        pos_mask = (torch.triu(torch.ones(labels.size(1),
                                          labels.size(1))) == 1).transpose(
                                              0, 1).unsqueeze(0)
        pos_mask = pos_mask.float().masked_fill(pos_mask == 0,
                                                float('-inf')).masked_fill(
                                                    pos_mask == 1, float(0.0))
        pad_mask = labels == self.vocabulary[self.pad_token]
        return pos_mask, pad_mask
