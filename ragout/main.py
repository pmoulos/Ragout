#(c) 2013-2014 by Authors
#This file is a part of Ragout program.
#Released under the BSD license (see LICENSE file)

"""
The main Ragout module. It defines top-level logic of the program
"""

import os
import sys
import shutil
import logging
import argparse
from collections import namedtuple
from copy import deepcopy

import ragout.assembly_graph.assembly_refine as asref
import ragout.assembly_graph.assembly_graph as asgraph
import ragout.scaffolder.scaffolder as scfldr
import ragout.scaffolder.merge_iters as merge
import ragout.scaffolder.output_generator as out_gen
import ragout.maf2synteny.maf2synteny as m2s
import ragout.overlap.overlap as overlap
import ragout.shared.config as config
from ragout.overlap.overlap import OverlapException
from ragout.phylogeny.phylogeny import Phylogeny, PhyloException
from ragout.breakpoint_graph.permutation import (PermutationContainer,
                                                 PermException)
from ragout.synteny_backend.synteny_backend import (SyntenyBackend,
                                                    BackendException)
from ragout.parsers.recipe_parser import parse_ragout_recipe, RecipeException
from ragout.parsers.fasta_parser import read_fasta_dict, FastaError
from ragout.shared.debug import DebugConfig
from ragout.breakpoint_graph.breakpoint_graph import BreakpointGraph
from ragout.breakpoint_graph.inferer import AdjacencyInferer
from ragout.breakpoint_graph.refiner import AdjacencyRefiner
from ragout.breakpoint_graph.chimera_detector import ChimeraDetector

#register backends
import synteny_backend.sibelia
import synteny_backend.cactus
import synteny_backend.maf
import synteny_backend.hal

logger = logging.getLogger()
debugger = DebugConfig.get_instance()


RunStage = namedtuple("RunStage", ["name", "block_size", "indels",
                                   "repeats", "rearrange", "refine"])


def enable_logging(log_file, debug):
    """
    Turns on logging, sets debug levels and assigns a log file
    """
    log_formatter = logging.Formatter("[%(asctime)s] %(name)s: %(levelname)s: "
                                      "%(message)s", "%H:%M:%S")
    console_formatter = logging.Formatter("[%(asctime)s] %(levelname)s: "
                                          "%(message)s", "%H:%M:%S")
    console_log = logging.StreamHandler()
    console_log.setFormatter(console_formatter)
    if not debug:
        console_log.setLevel(logging.INFO)

    file_handler = logging.FileHandler(log_file, mode="w")
    file_handler.setFormatter(log_formatter)

    logger.setLevel(logging.DEBUG)
    logger.addHandler(console_log)
    logger.addHandler(file_handler)


def check_extern_modules(backend):
    """
    Checks if all neccessary native modules are available
    """
    backends = SyntenyBackend.get_available_backends()
    if backend not in backends:
        raise BackendException("\"{0}\" is not installed.".format(backend))

    if not m2s.check_binary():
        raise BackendException("maf2synteny binary is missing, "
                               "did you run 'make'?")

    if not overlap.check_binary():
        raise BackendException("overlap binary is missing, "
                               "did you run 'make'?")


def run_ragout(args):
    """
    A wrapper to catch possible exceptions
    """
    try:
        run_unsafe(args)
    except (RecipeException, PhyloException, PermException,
            BackendException, OverlapException, FastaError) as e:
        logger.error("An error occured while running Ragout:")
        logger.error(e)
        return 1

    return 0


def make_run_stages(block_sizes, resolve_repeats):
    stages = []
    for block in block_sizes:
        stages.append(RunStage(str(block), block, False,
                      False, True, False))
    refine = True
    stages.append(RunStage("refine", block_sizes[-1], True,
                           resolve_repeats, False, refine))
    return stages


def run_unsafe(args):
    """
    Top-level logic of the program
    """
    out_log = os.path.join(args.out_dir, "ragout.log")
    out_overlap = os.path.join(args.out_dir, "contigs_overlap.dot")
    debug_root = os.path.join(args.out_dir, "debug")

    if not os.path.isdir(args.out_dir):
        os.mkdir(args.out_dir)
    if args.debug:
        debugger.set_debugging(True)
        debugger.set_debug_dir(debug_root)
        debugger.clear_debug_dir()

    enable_logging(out_log, args.debug)
    logger.info("Cooking Ragout...")

    check_extern_modules(args.synteny_backend)
    all_backends = SyntenyBackend.get_available_backends()
    backend = all_backends[args.synteny_backend]
    recipe = parse_ragout_recipe(args.recipe)

    #Setting synteny block sizes
    if "blocks" in recipe:
        scale = recipe["blocks"]
    else:
        scale = backend.infer_block_scale(recipe)
        logger.info("Synteny block scale set to '{0}'".format(scale))
    synteny_blocks = config.vals["blocks"][scale]

    #Running backend to get synteny blocks
    perm_files = backend.make_permutations(recipe, synteny_blocks, args.out_dir,
                                           args.overwrite, args.threads)
    run_stages = make_run_stages(synteny_blocks, args.resolve_repeats)

    #####
    #phylogeny-related
    if "tree" in recipe:
        logger.info("Phylogeny is taken from the recipe")
        phylogeny = Phylogeny.from_newick(recipe["tree"])
    else:
        logger.info("Inferring phylogeny from synteny blocks data")
        small_blocks = synteny_blocks[-1]
        simple_perm = PermutationContainer(perm_files[small_blocks],
                                           recipe, False, False, None)
        phylogeny = Phylogeny.from_permutations(simple_perm)
        logger.info(phylogeny.tree_string)
    ####

    ##Building chimera detector
    raw_bp_graphs = {}
    perms = {}
    for stage in run_stages:
        debugger.set_debug_dir(os.path.join(debug_root, stage.name))
        perms[stage] = PermutationContainer(perm_files[stage.block_size],
                            recipe, stage.repeats, not stage.indels, phylogeny)
        raw_bp_graphs[stage] = BreakpointGraph(perms[stage])
    chim_detect = ChimeraDetector(raw_bp_graphs, run_stages)
    ##

    #####
    scaffolds = None
    prev_stages = []
    for stage in run_stages:
        logger.info("Stage \"{0}\"".format(stage.name))
        debugger.set_debug_dir(os.path.join(debug_root, stage.name))
        prev_stages.append(stage)

        if not stage.refine:
            fixed_container = chim_detect.break_contigs(perms[stage], [stage])
            breakpoint_graph = BreakpointGraph(fixed_container)

            adj_inferer = AdjacencyInferer(breakpoint_graph, phylogeny)
            adjacencies = adj_inferer.infer_adjacencies()
            cur_scaffolds = scfldr.build_scaffolds(adjacencies, fixed_container)

            if scaffolds is not None:
                all_breaks = chim_detect.break_contigs(perms[stage], prev_stages)
                scaffolds = merge.merge_scaffolds(scaffolds, cur_scaffolds,
                                                  all_breaks, stage.rearrange)
            else:
                scaffolds = cur_scaffolds
        else:
            all_breaks = chim_detect.break_contigs(perms[stage], prev_stages)
            refine_bg = BreakpointGraph(all_breaks)
            adj_refiner = AdjacencyRefiner(refine_bg, phylogeny, all_breaks)
            scaffolds = merge.refine_scaffolds(scaffolds, adj_refiner,
                                               all_breaks)
        scfldr.assign_scaffold_names(scaffolds, perms[stage],
                                     recipe["references"][0])
    ####

    if args.debug:
        debugger.set_debug_dir(debug_root)

    logger.info("Reading contigs file")
    target_fasta_file = backend.get_target_fasta()
    target_fasta_dict = read_fasta_dict(target_fasta_file)

    if args.no_refine:
        out_gen.make_output(target_fasta_dict, scaffolds, args.out_dir)
    else:
        overlap.make_overlap_graph(target_fasta_file, out_overlap)
        refined_scaffolds = asref.refine_scaffolds(out_overlap, scaffolds,
                                                   target_fasta_dict)
        out_gen.make_output(target_fasta_dict, refined_scaffolds, args.out_dir)
        if args.debug:
            shutil.copy(out_overlap, debugger.debug_dir)
            out_colored_overlap = os.path.join(debugger.debug_dir,
                                               "colored_overlap.dot")
            asgraph.save_colored_insert_overlap_graph(out_overlap, scaffolds,
                                                      refined_scaffolds,
                                                      out_colored_overlap)
        os.remove(out_overlap)

    logger.info("Your Ragout is ready!")


def main():
    parser = argparse.ArgumentParser(description="Comparative assembly"
                                                 " with multiple references",
                                     formatter_class= \
                                        argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument("recipe", metavar="recipe_file",
                        help="path to recipe file")
    parser.add_argument("-o", "--outdir", dest="out_dir",
                        metavar="output_dir",
                        help="path to the working directory",
                        default="ragout-out")
    parser.add_argument("-s", "--synteny", dest="synteny_backend",
                        default="sibelia",
                        choices=["sibelia", "cactus", "maf", "hal"],
                        help="backend for synteny block decomposition")
    parser.add_argument("--no-refine", action="store_true",
                        dest="no_refine", default=False,
                        help="disable refinement with assembly graph")
    parser.add_argument("--overwrite", action="store_true", default=False,
                        dest="overwrite",
                        help="overwrite existing synteny blocks")
    parser.add_argument("--repeats", action="store_true", default=False,
                        dest="resolve_repeats",
                        help="try to resolve repeats before constructing "
                             "breakpoint graph")
    parser.add_argument("--debug", action="store_true",
                        dest="debug", default=False,
                        help="enable debug output")
    parser.add_argument("-t", "--threads", dest="threads", type=int,
                        default=1, help="number of threads for synteny backend")
    parser.add_argument("--version", action="version", version="Ragout v1.1")
    args = parser.parse_args()

    return run_ragout(args)
