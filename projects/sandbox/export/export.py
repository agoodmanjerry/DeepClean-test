import logging
from pathlib import Path
from typing import Callable, Iterable, Optional, Union

import torch

import hermes.quiver as qv
from deepclean.architectures import architecturize
from deepclean.export import PrePostDeepClean
from deepclean.gwftools.channels import ChannelList, get_channels
from deepclean.logging import configure_logging


def make_ensemble(
    repo: qv.ModelRepository,
    deepclean: qv.Model,
    ensemble_name: str,
    stream_size: int,
    num_updates: int,
    batch_size: int,
    snapshotter: Optional[qv.Model] = None,
    streams_per_gpu: int = 1,
) -> qv.Model:
    """
    Create an ensemble model in the indicated model `repo`
    built around a trained `deepclean` network which will
    aggregate its overlapping outputs of size `stream_size`
    over `num_updates` steps.

    Args:
        repo:
            The model repository in which to create this ensemble model
        deepclean:
            The deepclean model to use for creating noise predictions
        ensemble_name:
            The name to give this ensemble model
        stream_size:
            The size of the stream, in samples, the snapshotter
            should expect to receive and which the aggregator should
            return
        num_updates:
            The number of update steps over which the aggregator
            should perform online averaging before streaming back
            a prediction
        snapshotter:
            An existing snapshotter model to place at the front of
            the ensemble. If `None`, a new snapshotter model will
            be created
        streams_per_gpu:
            The number of snapshot and aggregator instances to
            host on each GPU at inference time
    """

    try:
        # first see if we have an existing
        # ensemble with the given name
        ensemble = repo.models[ensemble_name]
    except KeyError:
        # if we don't, create one and add a snapshotter
        # instance to it, possibly creating one if need be
        ensemble = repo.add(ensemble_name, platform=qv.Platform.ENSEMBLE)

        if snapshotter is None:
            # there's no snapshotter, so make one
            ensemble.add_streaming_inputs(
                inputs=[deepclean.inputs["witness"]],
                stream_size=stream_size,
                name="snapshotter",
                streams_per_gpu=streams_per_gpu,
                batch_size=batch_size,
            )
        else:
            # pipe the output of the existing snapshotter
            # model to DeepClean's witness input
            stream = list(snapshotter.inputs.values())[0]
            snapshot = list(snapshotter.outputs.values())[0]
            ensemble.add_input(stream)
            ensemble.pipe(snapshot, deepclean.inputs["witness"])
    else:
        # if there does already exist an ensemble by
        # the given name, make sure it has DeepClean
        # and the snapshotter as a part of its models
        if deepclean not in ensemble.models:
            raise ValueError(
                "Ensemble model '{}' already in repository "
                "but doesn't include model 'deepclean'".format(ensemble_name)
            )
        elif snapshotter is None or snapshotter not in ensemble.models:
            raise ValueError(
                "Ensemble model '{}' already in repository "
                "but doesn't include model 'snapshotter'".format(ensemble_name)
            )

    # keep snapshot states around for a long time in case
    # there are unexpected bottlenecks which throttle updates
    # for a few seconds
    snapshotter = repo.models["snapshotter"]
    snapshotter.config.sequence_batching.max_sequence_idle_microseconds = int(
        6e7
    )
    snapshotter.config.write()

    # add a streaming output to the model if it
    # doesn't already have one
    if len(ensemble.config.output) == 0:
        if ensemble_name == "deepclean-stream":
            name = "aggregator"
        else:
            name = "aggregator-" + ensemble.name.replace(
                "deepclean-stream-", ""
            )
        ensemble.add_streaming_output(
            deepclean.outputs["noise"],
            update_size=stream_size,
            num_updates=num_updates,
            batch_size=batch_size,
            name=name,
            streams_per_gpu=streams_per_gpu,
        )
        aggregator = repo.models[name]
        aggregator.config.sequence_batching.max_sequence_idle_microseconds = (
            int(6e7)
        )
        aggregator.config.write()

    # export the ensemble model, which basically amounts
    # to writing its config and creating an empty version entry
    ensemble.export_version(None)
    return ensemble


@architecturize
def export(
    architecture: Callable,
    repository_directory: str,
    output_directory: Path,
    channels: ChannelList,
    kernel_length: float,
    sample_rate: float,
    inference_sampling_rate: float,
    batch_size: int,
    max_latency: Union[float, Iterable[float]],
    weights: Optional[Path] = None,
    streams_per_gpu: int = 1,
    instances: Optional[int] = None,
    platform: qv.Platform = qv.Platform.ONNX,
    verbose: bool = False,
) -> None:
    """
    Export a DeepClean architecture to model repository
    for streaming inference, including adding models for
    caching input snapshot state as well as aggregated
    output state.

    Args:
        architecture:
            A function which takes as input a number of witness
            channels and returns an instantiated torch `Module`
            which represents a DeepClean network architecture
        repository_directory:
            Directory to which to save the models and their
            configs
        output_directory:
            Path to save logs. If `weights` is `None`, this
            directory is assumed to contain a file `"weights.pt"`.
        channels:
            A list of channel names used by DeepClean, with the
            strain channel first, or the path to a text file
            containing this list separated by newlines
        kernel_length:
            The length, in seconds, of the input to DeepClean
        stride_length:
            The length, in seconds, between kernels sampled
            at inference time. This, along with the `sample_rate`,
            dictates the size of the update expected at the
            snapshotter model
        sample_rate:
            Rate at which the input kernel has been sampled, in Hz
        max_latency:
            The maximum amount of time, in seconds, allowed during
            inference to wait for overlapping predictcions for
            online averaging. For example, if the `stride_length`
            is 0.002s and `max_latency` is 0.5s, then output segments
            will be averaged over 250 overlapping kernels before
            being streamed back from the server. This means there is
            a delay of `max_latency` (or the greatest multiple
            of `stride_length` that is less than `max_latency`) seconds
            between the start timestamp of the update streamed to
            the snapshotter and the resulting prediction returned by
            the ensemble model.
        weights:
            Path to a set of trained weights with which to
            initialize the network architecture. If left as
            `None`, a file called `"weights.pt"` will be looked
            for in the `output_directory`.
        streams_per_gpu:
            The number of snapshot states to host per GPU during
            inference
        instances:
            The number of concurrent execution instances of the
            DeepClean architecture to host per GPU during inference
        platform:
            The backend framework platform used to host the
            DeepClean architecture on the inference service. Right
            now only `"onnxruntime_onnx"` is supported.
        verbose:
            If set, log at `DEBUG` verbosity, otherwise log at
            `INFO` verbosity.
    """

    # load the channel names from a file if necessary
    channels = get_channels(channels)
    kernel_size = int(kernel_length * sample_rate)
    stride = int(sample_rate / inference_sampling_rate)

    # if we didn't specify a weights filename, assume
    # that a "weights.pt" lives in our output directory
    if weights is None:
        weights = output_directory / "weights.pt"
    if not weights.exists():
        raise FileNotFoundError(f"No weights file '{weights}'")

    configure_logging(output_directory / "export.log", verbose)

    # instantiate the architecture and initialize
    # its weights with the trained values
    logging.info(f"Creating model and loading weights from {weights}")
    nn = architecture(len(channels) - 1)
    nn = PrePostDeepClean(nn)
    nn.load_state_dict(torch.load(weights))
    nn.eval()

    # instantiate a model repository at the
    # indicated location and see if a deepclean
    # model already exists in this repository
    repo = qv.ModelRepository(repository_directory)
    try:
        model = repo.models["deepclean"]

        # if the model exists already and we specified
        # a concurrent instance count, scale the existing
        # instance group to this value
        if instances is not None:
            model.config.scale_instance_group(instances)
    except KeyError:
        # otherwise create the model using the indicated
        # platform and set up an instance group with the
        # indicated scale if one was provided
        model = repo.add("deepclean", platform=platform)
        if instances is not None:
            model.config.add_instance_group(count=instances)

    # export this version of the model (with its current
    # weights), to this entry in the model repository
    input_shape = (batch_size, len(channels) - 1, kernel_size)
    model.export_version(
        nn, input_shapes={"witness": input_shape}, output_names=["noise"]
    )

    # if we indicated multiple max_latency values,
    # create an ensemble model for each with a different
    # aggregator and index the ensemble names using
    # the max latency value. Otherwsie, just call the
    # ensemble "deepclean-stream"
    if isinstance(max_latency, float) or len(max_latency) == 1:
        if isinstance(max_latency, float):
            max_latency = [max_latency]
        ensemble_name = "deepclean-stream"
    else:
        ensemble_name = "deepclean-stream-{}"

    for latency in max_latency:
        name = ensemble_name.format(latency)
        num_updates = int(latency * inference_sampling_rate)
        logging.info(
            "Creating ensemble model {} which averages outputs "
            "over {} updates".format(name, num_updates)
        )

        make_ensemble(
            repo,
            model,
            name,
            stream_size=stride,
            num_updates=num_updates,
            batch_size=batch_size,
            snapshotter=repo.models.get("snapshotter", None),
            streams_per_gpu=streams_per_gpu,
        )


if __name__ == "__main__":
    export()
