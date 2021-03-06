#!/usr/bin/env python
# -*- coding: utf-8 -*-

#   PyNAM -- Python Neural Associative Memory Simulator and Evaluator
#   Copyright (C) 2015 Andreas Stöckel
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Main program of the PyNAM framework. This program can operate in different
modes: It can run a complete experiment, including network creation, execution
and analysis.
"""

import gzip
import tarfile
import pickle

import os.path
import time
import datetime

import logging
import subprocess
import multiprocessing

import numpy as np
import pynam
import pynam.entropy
import pynam.utils
import pynnless as pynl
import scipy.io as scio
import sys

# Get a logger, write to stderr
logger = logging.getLogger("PyNAM")
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

def help():
    print("""
PyNAM -- Python Neural Associative Memory Simulator and Evaluator

Copyright (C) 2015 Andreas Stöckel

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.


Usage:

To run the complete analysis pipeline, run the program in one of the following
forms:

    ./run.py <SIMULATOR> [<EXPERIMENT>]
    ./run.py <SIMULATOR> --process <EXPERIMENT>
    ./run.py <SIMULATOR> --process-keep <EXPERIMENT>

Where `SIMULATOR` is the simulator that should be used for execution (see below)
and `EXPERIMENT` is a JSON file describing the experiment that should be executed.
If `EXPERIMENT` is not given, the program will try to execute "experiment.json".
The "process-keep" mode will keep all intermediate spiking output. The
"process-keep" mode is equivalent to calling `./run` with "--create", "--exec"
and "--analyse" in a row.

In order to just generate the network descriptions for a specific experiment,
use the following format:

    ./run.py <SIMULATOR> --create <EXPERIMENT>

Note that the `SIMULATOR` is needed in order to partition the experiment
according to the hardware resources. This command will generate a set of
".in.gz" files that can be passed to the execution stage:

    ./run.py <SIMULATOR> --exec <IN_1> ... <IN_N>

This command will execute the network simulations for the given network
descriptors -- if more than one network descriptor is given, a new process
will be spawned for each execution. Generates a series of ".out.gz" files
in the same directory as the input files.

    ./run.py <TARGET> --analyse <OUT_1> ... <OUT_N>

Analyses the given output files, generates a HDF5/Matlab file as `TARGET`
containing the processed results.

If the expected output data is expected to be large the following commands can
be used:

    ./run.py <SIMULATOR> --analyse-exec <IN_1> ... <IN_N>
    ./run.py <TARGET> --analyse-join <OUT_1> ... <OUT_N>

The "analyse-exec" mode will directly generate HDF5/Matlab files after
each execution. These files are much smaller than the intermediate files
generated by "exec". All results can then be joined using the "analyse-join"
mode.

""")
    print("<SIMULATOR> may be one of the following, these simulators have been")
    print("auto-detected on your system:")
    print("\t" + str(pynl.PyNNLess.simulators()))
    sys.exit(1)

def short_help():
    print("Type\n\t./run.py --help\nto get usage information.")
    sys.exit(1)

def parse_parameters():
    # Error case: Need at least one argument
    if len(sys.argv) == 1:
        print("Error: At least one argument is required")
        short_help()

    # Make sure the first argument does not start with "--"
    if len(sys.argv) >= 2 and sys.argv[1].startswith("--"):
        if (sys.argv[1] == '-h' or sys.argv[1] == '--help'):
            help()
        print("Error: Invalid arguments")
        short_help()

    # Special case -- only one argument is given -- print help if the first
    # argument is "-h" or "--help"
    if len(sys.argv) == 2:
        return {
            "mode": "process",
            "simulator": sys.argv[1],
            "experiment": "experiment.json"
        }


    # Special case two: Three parameters are given and the third parameter does
    # not start with "--"
    if len(sys.argv) == 3 and not sys.argv[2].startswith("--"):
        return {
            "mode": "process",
            "simulator": sys.argv[1],
            "experiment": sys.argv[2]
        }

    # We need at least three parameters and the second parameter needs to be
    # in ["process", "create", "exec", "analyse"]
    if len(sys.argv) < 3 or not sys.argv[2].startswith("--"):
        print("Error: Invalid arguments")
        short_help()
    mode = sys.argv[2][2:]
    modes = ["process", "process-keep", "create", "exec", "analyse",
                "analyse-exec", "analyse-join"]
    if not mode in modes:
        print("Error: Processing mode must be one of " + str(modes) + ", but \""
                + mode + "\" was given")
        short_help()

    # Require at least one argument
    if len(sys.argv) < 4:
        print("Error: At least one argument is required for mode \"" + mode  +
                "\"")
        short_help()

    # Parse the "process" mode
    if mode == "process" or mode == "process-keep" or mode == "create":
        if len(sys.argv) != 4:
            print("Error: Mode \"" + mode + "\" requires exactly one argument")
            short_help()
        else:
            return {
                "mode": mode,
                "simulator": sys.argv[1],
                "experiment": sys.argv[3]
            }

    # Parse the "create" and "exec" modes
    if mode == "exec" or mode == "analyse-exec":
        return {
            "mode": mode,
            "simulator": sys.argv[1],
            "files": sys.argv[3:]
        }

    # Parse the "analyse" mode
    if mode == "analyse" or mode == "analyse-join":
        return {
            "mode": mode,
            "target": sys.argv[1],
            "files": sys.argv[3:]
        }

    # Something went wrong, print the usage help
    short_help()

def validate_parameters(params):
    # Make sure the specified input files exist
    files = []
    if "experiment" in params:
        files.append(params["experiment"])
    if "files" in params:
        files = files + params["files"]
    for fn in files:
        if not os.path.isfile(fn):
            print "Error: Specified file \"" + fn + "\" does not exist."
            short_help()

def read_experiment(experiment_file):
    return pynam.Experiment.read_from_file(experiment_file)

def write_object(obj, filename):
    with gzip.open(filename, 'wb') as f:
        pickle.dump(obj, f)

def read_object(filename):
    with gzip.open(filename, 'rb') as f:
        return pickle.load(f)

def create_networks(experiment_file, simulator, path="", analyse=False):
    """
    Create the network descriptions and write them to disc. Returns the names
    of the created files, separated by experiment
    """

    # Read the experiment
    experiment = read_experiment(experiment_file)
    experiment_name = os.path.basename(experiment_file)
    if experiment_name.find('.') > -1:
        experiment_name = experiment_name[0:experiment_name.find('.')]

    # Build the experiment descriptors
    seed = 1437243
    logger.info("Generating networks...")
    pools = experiment.build(pynl.PyNNLess.get_simulator_info_static(simulator),
            simulator=simulator, seed=seed)

    # Create the target directories
    if path != "" and not os.path.isdir(path):
        os.makedirs(path)

    # Store the descriptors in the given path
    input_files = []
    output_files = []
    for i, pool in enumerate(pools):
        in_filename = os.path.join(path, experiment_name + "_" + str(i)
                + ".in.gz")
        out_ext = ".out.mat" if analyse else ".out.gz"
        out_filename = os.path.join(path, experiment_name + "_" + str(i)
                + out_ext)

        logger.info("Writing network descriptor to: " + in_filename)
        write_object(pool, in_filename)

        input_files.append(in_filename)
        output_files.append(out_filename)
    return input_files, output_files

def execute_networks(input_files, simulator, analyse=False):
    # If there is more than one input_file, execute these in different processes
    if (len(input_files) > 1):
        # Always spawn as many child processes as CPUs. The PyNNLessIsolated
        # class will care for serialization in the case of hardware systems.
        if pynl.PyNNLess.normalized_simulator_name(simulator) == "nmpm1":
            concurrency = 1 # The pyhmf hardware backend only allows a single
                            # concurrent instance of the PyNN process
        else:
            concurrency = multiprocessing.cpu_count()

        # Assemble the processes that should be executed
        mode = "--analyse-exec" if analyse else "--exec"
        script = os.path.realpath(__file__)
        cmds = [[sys.executable, script, simulator, mode, x] for x in input_files]
        processes = []
        had_error = False
        while (((len(cmds) > 0) or (len(processes) > 0))
                and not (len(processes) == 0 and had_error)):
            # Spawn new processes
            if ((len(processes) < concurrency) and (not had_error)
                    and (len(cmds) > 0)):
                cmd = cmds.pop()
                logger.info("Executing " + " ".join(cmd))
                processes.append(subprocess.Popen(cmd))

            # Check whether any of the processes has finished
            for i, process in enumerate(processes):
                if not process.poll() is None:
                    if process.returncode != 0:
                        had_error = True
                    del processes[i]
                    continue

            # Sleep a short while before rechecking
            time.sleep(0.1)

        # Return whether the was an error during execution
        if had_error:
            logger.error("There was an error during network execution!")
        return not had_error

    # Fetch the input file
    input_file = input_files[0]

    # Generate the output file name
    if (input_file.endswith(".in.gz")):
        output_file = input_file[:-6] + ".out.gz"
    else:
        output_file = input_file + ".out.gz"

    # Read the input file
    logger.info("Reading input file: " + input_file)
    input_network = read_object(input_file)

    # Redirect IO if the simulator is NMPM1
    normalized_simulator = pynl.PyNNLess.normalized_simulator_name(simulator)
    setup = {
        "redirect_io": True,#normalized_simulator == "nmpm1"
        "summarise_io": True
    }

    logger.info("Run simulation...")
    sim = pynl.PyNNLessIsolated(simulator, setup)
    output = sim.run(input_network)
    times = sim.get_time_info()
    logger.info("Simulation took " + str(times["sim"]) + "s ("
            + str(times["total"]) + "s total)")

    # Assemble the result object
    result = {
            "pool": input_network,
            "times": times,
            "output": output
        }

    # If the corresponding flag is given, analyse the result first
    if analyse:
        output_file = output_file[:-7] + ".out.mat"
        result = analyse_output(result)

    # Save the output, either as HDF5 or as compressed Pickle
    logger.info("Writing output file: " + output_file)
    if analyse:
        scio.savemat(output_file, result, do_compression=True, oned_as='row')
    else:
        write_object(result, output_file)

    return True

def analyse_output(output, result=None):
    # Create the result object if none was given
    if result is None:
        result = {}

    # Fetch the network pool from the given output instance
    pool = pynam.NetworkPool(output["pool"])
    times = output["times"]

    # Split the output into multiple chunks
    logger.info("Demultiplexing...")
    analysis_instances = pool.build_analysis(output["output"])

    # Iterate over the analysis instances -- calculate all metrics
    for i, analysis in enumerate(analysis_instances):
        # Create a result entry if it does not exist yet
        meta_data = analysis["meta_data"]
        name = meta_data["experiment_name"]
        if not name in result:
            keys = meta_data["keys"] + ["I", "I_n", "I_ref", "fp",
                    "fp_n", "fp_ref", "fp_ref_n", "fn", "fn_n", 
                    "lat_avg", "lat_std", "n_lat_inv"]
            result[name] = {
                "keys": keys,
                "dims": len(meta_data["keys"]),
                "simulator": meta_data["simulator"],
                "time": {
                    "total": 0,
                    "sim": 0,
                    "initialize": 0,
                    "finalize": 0
                },
                "data": np.zeros((meta_data["experiment_size"], len(keys))),
                "idx": 0
            }

        # Increment the time needed for this simulation (but only once per
        # simulation)
        if i == 0:
            for key in times:
                t = times[key]
                result[name]["time"][key] = result[name]["time"][key] + t

        # Fetch the values that have been varied
        params = {
            "data": analysis["data_params"],
            "topology": analysis["topology_params"],
            "input": analysis["input_params"],
            "output": pynam.OutputParameters(meta_data["output_params"])
        }
        values = np.zeros(len(result[name]["keys"]))
        for j, key in enumerate(meta_data["keys"]):
            parts = key.split('.')
            value = params
            for part in parts:
                value = value[part]
            values[j] = value

        # Calculate all metrics
        if i == 0 or (i + 1) % 50 == 0 or i + 1 == len(analysis_instances):
            logger.info("Calculating metrics (" + str(i + 1) + "/"
                    + str(len(analysis_instances)) + ")...")
        I, mat, errs = analysis.calculate_storage_capactiy(
                output_params=params["output"])
        I_ref, mat_ref, errs_ref = analysis.calculate_max_storage_capacity()
        fp = sum(map(lambda x: x["fp"], errs))
        fp_ref = sum(map(lambda x: x["fp"], errs_ref))
        fn = sum(map(lambda x: x["fn"], errs))
        latencies = analysis.calculate_latencies()
        latencies_valid = latencies[latencies != np.inf]
        latencies_invalid_count = len(latencies) - len(latencies_valid)
        if len(latencies_valid) > 0:
            latency_mean = np.mean(latencies_valid)
            latency_std = np.std(latencies_valid)
        else:
            latency_mean = np.inf
            latency_std = 0

        # Store the metrics
        offs = result[name]["dims"]
        dp = params["data"]
        norm_fp = dp["n_samples"] * (dp["n_bits_out"] - dp["n_ones_out"])
        norm_fn = dp["n_samples"] * dp["n_ones_out"]
        values[offs + 0] = I
        values[offs + 1] = 0.0 if I_ref == 0.0 else I / float(I_ref)
        values[offs + 2] = I_ref
        values[offs + 3] = fp
        values[offs + 4] = 0.0 if norm_fp == 0.0 else fp / float(norm_fp)
        values[offs + 5] = fp_ref
        values[offs + 6] = 0.0 if norm_fp == 0.0 else fp_ref / float(norm_fp)
        values[offs + 7] = fn
        values[offs + 8] = 0.0 if norm_fn == 0.0 else fn / float(norm_fn)
        values[offs + 9] = latency_mean
        values[offs + 10] = latency_std
        values[offs + 11] = latencies_invalid_count

        # Store the row in the result matrix for this experiment
        result[name]["data"][result[name]["idx"]] = values
        result[name]["idx"] = result[name]["idx"] + 1

    return result

def append_analysis(analysis, analysis_part):
    """
    Appends analysis_part to analysis and returns the result.
    """

    for experiment in analysis_part:
        # Skip internal values
        if experiment.startswith('__'):
            continue

        # If the experiment is not yet in the result
        if not experiment in analysis:
            analysis[experiment] = analysis_part[experiment]
        else:
            d1 = analysis[experiment]["data"]
            d2 = analysis_part[experiment]["data"]
            idx1 = analysis[experiment]["idx"]
            idx2 = analysis_part[experiment]["idx"]
            d1[idx1:(idx1+idx2), :] = d2[:idx2, :]
            analysis[experiment]["idx"] = idx1 + idx2
    return analysis

def finalize_analysis(result):
    # Sort the result matrices and remove the temporary "idx" key
    for name in result:
        res = result[name]
        if result[name]["dims"] > 0:
            if (len(res["data"].shape) == 1):
                res["data"] = res["data"].reshape((1, res["data"].size))
            sort_keys = res["data"][:, 0:result[name]["dims"]].T
            res["data"] = res["data"][np.lexsort(sort_keys)]
        if res["idx"] == 1:
            data = res["data"]
            if len(data.shape) > 1:
                data = data[0]
            # If there is only one result row, it is likely that we just want
            # to just evaluate the network. Print the keys and the results.
            print
            print "Results for experiment \"" + name + "\""
            print "\t".join(map(lambda x: x.strip(), res["keys"]))
            print "\t".join(map(lambda x: "%.2f" % x, data))
            print
            print "Timings for experiment \"" + name + "\""
            for k, v in res["time"].items():
                print (k
                    + " " * (max(map(len, res["time"].keys())) - len(k)) + "\t"
                    + str(v))
            print
        del res["idx"]
    return result

def analysis_join_output_files(output_files, target, folder=""):
    # Load all parts independently and join them
    analysis = {}
    for output_file in output_files:
        logger.info("Reading output file: " + output_file)
        analysis_part = pynam.utils.loadmat(output_file)
        analysis = append_analysis(analysis, analysis_part)

    # Store the result in a HDF5/Matlab file
    target = os.path.join(folder, target)
    logger.info("Writing target file: " + target)
    scio.savemat(target, finalize_analysis(analysis), do_compression=True,
            oned_as='row')

def analyse_output_files(output_files, target, folder=""):
    # Structure holding the final results, indexed by experiment name
    result = {}
    for output_file in output_files:
        # Read the result, recreate the network pool and get the analysis
        # instances
        logger.info("Reading output file: " + output_file)
        output = read_object(output_file)
        result = analyse_output(output, result)

    # Store the result in a HDF5/Matlab file
    target = os.path.join(folder, target)
    logger.info("Writing target file: " + target)
    scio.savemat(target, finalize_analysis(result), do_compression=True,
            oned_as='row')

#
# Main entry point -- parse and validate the parameters and depending on those,
# execute the corresponding functions defined above
#

if __name__ == "__main__":
    # Parse the parameters and validate them
    params = parse_parameters()
    validate_parameters(params)

    mode = params["mode"]

    if mode == "create":
        create_networks(params["experiment"], params["simulator"], "")
    elif mode == "exec" or mode == "analyse-exec":
        if not execute_networks(params["files"], params["simulator"],
                mode == "analyse-exec"):
            sys.exit(1)
    elif mode == "analyse":
        analyse_output_files(params["files"], params["target"])
    elif mode == "analyse-join":
        analysis_join_output_files(params["files"], params["target"])
    elif mode == "process" or mode == "process-keep":
        experiment = params["experiment"]
        simulator = params["simulator"]
        keep = mode == "process-keep"
        analyse = not keep

        # Assemble a directory for the experiment files
        date_prefix = datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
        folder = os.path.join("out", date_prefix + "_" + simulator)

        # Create the networks and fetch the input and output files
        input_files, output_files = create_networks(experiment, simulator, folder,
                analyse=analyse)

        # Execute the networks, abort if the execution failed
        if not execute_networks(input_files, simulator, analyse=analyse):
            sys.exit(1)

        # Assemble the name of the target HDF5 file
        experiment = os.path.basename(experiment)
        if experiment.endswith(".json"):
            experiment = experiment[:-5]
        target = date_prefix + "_" + simulator + "_" + experiment + ".mat"

        # Either just join the intermediate files or perform the actual analysis
        if analyse:
            analysis_join_output_files(output_files, target, "out")
        else:
            analyse_output_files(output_files, target, "out")

        # Remove the partial input and output files (no longer needed)
        if not keep:
            for input_file in input_files:
                try:
                    os.remove(input_file)
                except:
                    logger.warn("Error while deleting " + input_file)
            for output_file in output_files:
                try:
                    os.remove(output_file)
                except:
                    logger.warn("Error while deleting " + output_file)
            try:
                os.rmdir(folder)
            except:
                logger.warn("Error while deleting " + folder)

    logger.info("Done.")
    sys.exit(0)

