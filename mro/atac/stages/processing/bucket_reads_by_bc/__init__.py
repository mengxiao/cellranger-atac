#!/usr/bin/env python
#
# Copyright (c) 2019 10x Genomics, Inc. All rights reserved.
#
# Put BAM file into buckets by barcode prefix
#
import os.path
import random
import subprocess

import tenkit.bam as tk_bam
import tenkit.seq as tk_seq

from tools import create_bam_infile

from barcodes import get_read_barcode

__MRO__ = """
stage BUCKET_BY_BC(
    in  int      nbases,
    in  bam      input,
    out string[] qnames,
    out map      buckets,
    out bam,
    src py       "stages/processing/bucket_reads_by_bc",
) split using (
    in  string   chunk_start,
    in  string   chunk_end,
)
"""

def split(args):
    bam_in = create_bam_infile(args.input)
    chunk_defs = tk_bam.chunk_bam_records(bam_in, chunk_bound_key=None, chunk_size_gb=0.5)
    for chunk_def in chunk_defs:
        chunk_def['__mem_gb'] = 24
    return {'chunks': chunk_defs}

def bc_sort_key(read):
    return (get_read_barcode(read), read.qname)

def main(args, outs):
    main_bucket_reads_by_bc(args, outs)

def main_bucket_reads_by_bc(args, outs):
    chunk_start = args.chunk_start
    chunk_end = args.chunk_end

    prefixes = get_seqs(args.nbases)

    bam_in = create_bam_infile(args.input)

    buckets = {prefix: [] for prefix in prefixes}

    non_bc_bam_out, _ = tk_bam.create_bam_outfile(outs.default, None, None, template=bam_in)
    non_bc_reads = []
    for r in tk_bam.read_bam_chunk(bam_in, (chunk_start, chunk_end)):
        barcode = get_read_barcode(r, None)
        if barcode is None:
            non_bc_bam_out.write(r)
            non_bc_reads.append(r)
        else:
            prefix = barcode[:args.nbases]
            buckets[prefix].append(r)
    non_bc_bam_out.close()

    # Set random seed to get deterministic qname subsampling
    random.seed(0)
    sampled_non_bc_reads = random.sample(non_bc_reads, min(len(non_bc_reads), len(prefixes)))
    outs.qnames = [read.qname for read in sampled_non_bc_reads]

    outs.buckets = {}
    files_dir = os.path.dirname(outs.default)
    for prefix, bucket in buckets.iteritems():
        filename = os.path.join(files_dir, "bc_%s.bam" % prefix)
        outs.buckets[prefix] = filename
        bucket.sort(key=bc_sort_key)
        bam_out, _ = tk_bam.create_bam_outfile(filename, None, None, template=bam_in)
        try:
            for r in bucket:
                bam_out.write(r)
        finally:
            bam_out.close()
    bam_in.close()

def get_seqs(l):
    if l == 1:
        return tk_seq.NUCS
    old_seqs = get_seqs(l-1)
    new_seqs = []
    for old_seq in old_seqs:
        for base in tk_seq.NUCS:
            new_seqs.append(old_seq + base)
    return new_seqs

def join(args, outs, chunk_defs, chunk_outs):
    outs.coerce_strings()
    outs.buckets = {}
    outs.qnames = []
    for out in chunk_outs:
        outs.qnames += out.qnames
        for prefix, filename in out.buckets.iteritems():
            if prefix not in outs.buckets:
                outs.buckets[prefix] = []
            outs.buckets[prefix].append(filename)

    # Concatenate all non-barcode reads into single bam file for next bucketing pass
    if len(chunk_outs) == 1:
        subprocess.call(['mv', chunk_outs[0].default, outs.default])
    else:
        tk_bam.concatenate(outs.default, [out.default for out in chunk_outs])

    # Remove duplicate and sort all non-barcode qnames from chunks to determine qname bucket keys
    # for next bucketing pass
    outs.qnames = list(set(outs.qnames))
    outs.qnames.sort()
    n = int(len(outs.qnames) / len(outs.buckets))
    outs.qnames = [outs.qnames[i] for i in xrange(0, len(outs.qnames), max(1, n))]

    # Need to add the empty string to qnames to catch all qname strings before first qname bucket key
    outs.qnames.insert(0, '')
