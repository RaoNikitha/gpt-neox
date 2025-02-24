# Copyright (c) 2021, EleutherAI
# This file is based on code by the authors denoted below and has been modified from its original version.
#
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Processing data for pretraining."""

import argparse
import multiprocessing
import os
import random
import sys
sys.path.append('.')

#import lm_dataformat as lmd

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__),
                                             os.path.pardir)))
import time
import tqdm
import torch
import ftfy

from megatron.tokenizer import build_tokenizer
from megatron.data import indexed_dataset
from threading import Semaphore


class Encoder(object):
    def __init__(self, args):
        self.args = args

    def initializer(self):
        # Use Encoder class as a container for global data
        Encoder.tokenizer = build_tokenizer(self.args)

    def encode(self, text):
        if self.args.ftfy:
            text = ftfy.fix_text(text)
        ids = {}
        for key in self.args.json_keys:
            doc_ids = []
            text_ids = Encoder.tokenizer.tokenize(text)
            if len(text_ids) > 0:
                doc_ids.append(text_ids)
            if self.args.append_eod:
                doc_ids[-1].append(Encoder.tokenizer.eod)
            ids[key] = doc_ids
            #print('Encoded doc_ids', doc_ids)
            #print('decoded doc_ids', Encoder.tokenizer.detokenize(doc_ids[0]))
        return ids, len(text)
    
def get_args():
    parser = argparse.ArgumentParser()
    group = parser.add_argument_group(title='input data')
    group.add_argument('--input', type=str, required=True,
                       help='Path to input lmd archive(s) - if using multiple archives, put them in a comma separated '
                            'list')
    group.add_argument('--json-keys', nargs='+', default=['text'],
                       help='space separate listed of keys to extract from json')
    group.add_argument('--num-docs', default=None,
                       help='Number of documents in the input data (if known) for an accurate progress bar.', type=int)
    group = parser.add_argument_group(title='tokenizer')
    group.add_argument('--tokenizer-type', type=str, required=True,
                       choices=['HFGPT2Tokenizer', 'SPMTokenizer', 'HFTokenizer',
                                'GPT2BPETokenizer', 'CharLevelTokenizer'],
                       help='What type of tokenizer to use.')
    group.add_argument('--vocab-file', type=str, default=None,
                       help='Path to the vocab file')
    group.add_argument('--merge-file', type=str, default=None,
                       help='Path to the BPE merge file (if necessary).')
    group.add_argument('--append-eod', action='store_true',
                       help='Append an <eod> token to the end of a document.')
    group.add_argument('--ftfy', action='store_true',
                       help='Use ftfy to clean text')
    group = parser.add_argument_group(title='output data')
    group.add_argument('--output-prefix', type=str, required=True,
                       help='Path to binary output file without suffix')
    group.add_argument('--dataset-impl', type=str, default='mmap',
                       choices=['lazy', 'cached', 'mmap'])

    group = parser.add_argument_group(title='runtime')
    group.add_argument('--workers', type=int, default=1,
                       help='Number of worker processes to launch')
    group.add_argument('--log-interval', type=int, default=100,
                       help='Interval between progress updates')
    args = parser.parse_args()
    args.keep_empty = False

    # some default/dummy values for the tokenizer
    args.rank = 0
    args.make_vocab_size_divisible_by = 128
    args.model_parallel_size = 1

    return args


def sanitize(text):
    # Set up reserved tokens.
    SPACE_TOKEN = '▁'  # Note: not a regular underscore, but the SPM unicode symbol for a space
    TAB_TOKEN = chr(1)  # Assign rarely used ASCII chars for tabs and newlines. Can also pick rare unicode characters.
    NEWLINE_TOKEN = chr(2)
    text = text.replace('\n', NEWLINE_TOKEN)
    text = text.replace('\t', TAB_TOKEN)
    text = text.replace(' ', SPACE_TOKEN)
    return text


def yield_from_files(dir, semaphore):
    """
    Iterator over input documents using lm_dataformat. Should be able to handle jsons / texts /
    other compressed formats. Also filters out empty documents.

    :param fnames: list of filenames
    """
    fnames = []
    for root, _, files in os.walk(dir):
        for file in files:
            fnames.append(os.path.join(root, file))
    random.shuffle(fnames)

    def read(fname):
        with open(fname, encoding='utf-8', errors='ignore') as inp:
            doc = inp.read()
        doc = sanitize(doc)
        return doc

    def yielder(fname, semaphore):
        f = read(fname)
        if f:
            semaphore.acquire()
            yield f

    for fname in fnames:
        yield from yielder(fname, semaphore)


def main():
    args = get_args()
    encoder = Encoder(args)
    tokenizer = build_tokenizer(args)
    print(f"Vocab size: {tokenizer.vocab_size}")
    print(f"Output prefix: {args.output_prefix}")

    # build a semaphore object to stop `yield_from_files` from getting ahead of encoder.encode and
    # hence building up memory
    semaphore = Semaphore(10000 + args.workers)

    # use multiprocessing to iterate over input documents
    fin = yield_from_files(args.input, semaphore)

    if args.workers > 1:
        pool = multiprocessing.Pool(args.workers, initializer=encoder.initializer)
        encoded_docs = pool.imap(encoder.encode, fin, chunksize=25)
    else:
        encoder.initializer()
        encoded_docs = (encoder.encode(doc) for doc in fin)

    # make a dataset builder for each key in args.json_keys
    # each key will output to a different file beginning with args.output_prefix
    output_bin_files = {}
    output_idx_files = {}
    builders = {}
    for key in ("text",): #args.json_keys:
        output_bin_files[key] = "{}_{}_{}.bin".format(args.output_prefix,
                                                      key, "document")
        output_idx_files[key] = "{}_{}_{}.idx".format(args.output_prefix,
                                                      key, "document")
        builders[key] = indexed_dataset.make_builder(output_bin_files[key],
                                                     impl=args.dataset_impl,
                                                     vocab_size=tokenizer.vocab_size)

    # actually do tokenization
    proc_start = time.time()
    total_bytes_processed = 0
    pbar = tqdm.tqdm()
    for i, (doc, bytes_processed) in enumerate(encoded_docs, start=1):
        total_bytes_processed += bytes_processed
        # release semaphore so `yield_from_files` can add another file to the buffer
        semaphore.release()

        # add each tokenized document / sentence
        for key, sentences in doc.items():
            for sentence in sentences:
                builders[key].add_item(torch.IntTensor(sentence))
            # separate with eos token
            builders[key].end_document()

        # log progress
        if i % args.log_interval == 0:
            current = time.time()
            elapsed = current - proc_start
            mbs = total_bytes_processed / elapsed / 1024 / 1024
            pbar.set_description(
                f"Processed {i}{'' if args.num_docs is None else '/' + str(args.num_docs)} documents ({i / elapsed} docs/s, {mbs} MB/s).")
            if i != 0:
                pbar.update(args.log_interval)


    # save output file
    for key in args.json_keys:
        builders[key].finalize(output_idx_files[key])


if __name__ == '__main__':
    main()

# added eof tag
# sudo python3 tools/preprocess_data.py --input /data/GitHubMining/CurrentStateProcessed/test/ --tokenizer-type SPMTokenizer --vocab-file /code/Code/SoftwareTesting/FileLevel/DataPrep/vocabulary_10l.model --append-eod --output-prefix /data/GitHubMining/UpdatedTokenizedData/test/processed-test --workers 32
# sudo python3 tools/preprocess_data.py --input /data/GitHubMining/CurrentStateProcessed/train/ --tokenizer-type SPMTokenizer --vocab-file /code/Code/SoftwareTesting/FileLevel/DataPrep/vocabulary_10l.model --append-eod --output-prefix /data/GitHubMining/UpdatedTokenizedData/train/processed-train --workers 32