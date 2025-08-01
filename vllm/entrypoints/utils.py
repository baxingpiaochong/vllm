# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import argparse
import asyncio
import functools
import os
import subprocess
import sys
from typing import Any, Optional, Union

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask, BackgroundTasks

from vllm.entrypoints.openai.protocol import (ChatCompletionRequest,
                                              CompletionRequest)
from vllm.logger import init_logger
from vllm.platforms import current_platform

logger = init_logger(__name__)

VLLM_SUBCMD_PARSER_EPILOG = (
    "Tip: Use `vllm [serve|run-batch|bench <bench_type>] "
    "--help=<keyword>` to explore arguments from help.\n"
    "   - To view a argument group:     --help=ModelConfig\n"
    "   - To view a single argument:    --help=max-num-seqs\n"
    "   - To search by keyword:         --help=max\n"
    "   - To list all groups:           --help=listgroup\n"
    "   - To view help with pager:      --help=page")


async def listen_for_disconnect(request: Request) -> None:
    """Returns if a disconnect message is received"""
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            # If load tracking is enabled *and* the counter exists, decrement
            # it. Combines the previous nested checks into a single condition
            # to satisfy the linter rule.
            if (getattr(request.app.state, "enable_server_load_tracking",
                        False)
                    and hasattr(request.app.state, "server_load_metrics")):
                request.app.state.server_load_metrics -= 1
            break


def with_cancellation(handler_func):
    """Decorator that allows a route handler to be cancelled by client
    disconnections.

    This does _not_ use request.is_disconnected, which does not work with
    middleware. Instead this follows the pattern from
    starlette.StreamingResponse, which simultaneously awaits on two tasks- one
    to wait for an http disconnect message, and the other to do the work that we
    want done. When the first task finishes, the other is cancelled.

    A core assumption of this method is that the body of the request has already
    been read. This is a safe assumption to make for fastapi handlers that have
    already parsed the body of the request into a pydantic model for us.
    This decorator is unsafe to use elsewhere, as it will consume and throw away
    all incoming messages for the request while it looks for a disconnect
    message.

    In the case where a `StreamingResponse` is returned by the handler, this
    wrapper will stop listening for disconnects and instead the response object
    will start listening for disconnects.
    """

    # Functools.wraps is required for this wrapper to appear to fastapi as a
    # normal route handler, with the correct request type hinting.
    @functools.wraps(handler_func)
    async def wrapper(*args, **kwargs):

        # The request is either the second positional arg or `raw_request`
        request = args[1] if len(args) > 1 else kwargs["raw_request"]

        handler_task = asyncio.create_task(handler_func(*args, **kwargs))
        cancellation_task = asyncio.create_task(listen_for_disconnect(request))

        done, pending = await asyncio.wait([handler_task, cancellation_task],
                                           return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

        if handler_task in done:
            return handler_task.result()
        return None

    return wrapper


def decrement_server_load(request: Request):
    request.app.state.server_load_metrics -= 1


def load_aware_call(func):

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        raw_request = kwargs.get("raw_request",
                                 args[1] if len(args) > 1 else None)

        if raw_request is None:
            raise ValueError(
                "raw_request required when server load tracking is enabled")

        if not getattr(raw_request.app.state, "enable_server_load_tracking",
                       False):
            return await func(*args, **kwargs)

        # ensure the counter exists
        if not hasattr(raw_request.app.state, "server_load_metrics"):
            raw_request.app.state.server_load_metrics = 0

        raw_request.app.state.server_load_metrics += 1
        try:
            response = await func(*args, **kwargs)
        except Exception:
            raw_request.app.state.server_load_metrics -= 1
            raise

        if isinstance(response, (JSONResponse, StreamingResponse)):
            if response.background is None:
                response.background = BackgroundTask(decrement_server_load,
                                                     raw_request)
            elif isinstance(response.background, BackgroundTasks):
                response.background.add_task(decrement_server_load,
                                             raw_request)
            elif isinstance(response.background, BackgroundTask):
                # Convert the single BackgroundTask to BackgroundTasks
                # and chain the decrement_server_load task to it
                tasks = BackgroundTasks()
                tasks.add_task(response.background.func,
                               *response.background.args,
                               **response.background.kwargs)
                tasks.add_task(decrement_server_load, raw_request)
                response.background = tasks
        else:
            raw_request.app.state.server_load_metrics -= 1

        return response

    return wrapper


def cli_env_setup():
    # The safest multiprocessing method is `spawn`, as the default `fork` method
    # is not compatible with some accelerators. The default method will be
    # changing in future versions of Python, so we should use it explicitly when
    # possible.
    #
    # We only set it here in the CLI entrypoint, because changing to `spawn`
    # could break some existing code using vLLM as a library. `spawn` will cause
    # unexpected behavior if the code is not protected by
    # `if __name__ == "__main__":`.
    #
    # References:
    # - https://docs.python.org/3/library/multiprocessing.html#contexts-and-start-methods
    # - https://pytorch.org/docs/stable/notes/multiprocessing.html#cuda-in-multiprocessing
    # - https://pytorch.org/docs/stable/multiprocessing.html#sharing-cuda-tensors
    # - https://docs.habana.ai/en/latest/PyTorch/Getting_Started_with_PyTorch_and_Gaudi/Getting_Started_with_PyTorch.html?highlight=multiprocessing#torch-multiprocessing-for-dataloaders
    if "VLLM_WORKER_MULTIPROC_METHOD" not in os.environ:
        logger.debug("Setting VLLM_WORKER_MULTIPROC_METHOD to 'spawn'")
        os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"


def _validate_truncation_size(
    max_model_len: int,
    truncate_prompt_tokens: Optional[int],
    tokenization_kwargs: Optional[dict[str, Any]] = None,
) -> Optional[int]:

    if truncate_prompt_tokens is not None:
        if truncate_prompt_tokens <= -1:
            truncate_prompt_tokens = max_model_len

        if truncate_prompt_tokens > max_model_len:
            raise ValueError(
                f"truncate_prompt_tokens value ({truncate_prompt_tokens}) "
                f"is greater than max_model_len ({max_model_len})."
                f" Please, select a smaller truncation size.")

        if tokenization_kwargs is not None:
            tokenization_kwargs["truncation"] = True
            tokenization_kwargs["max_length"] = truncate_prompt_tokens

    else:
        if tokenization_kwargs is not None:
            tokenization_kwargs["truncation"] = False

    return truncate_prompt_tokens


def _output_with_pager(text: str):
    """Output text using scrolling view if available and appropriate."""

    pagers = ['less -R', 'more']
    for pager_cmd in pagers:
        try:
            proc = subprocess.Popen(pager_cmd.split(),
                                    stdin=subprocess.PIPE,
                                    text=True)
            proc.communicate(input=text)
            return
        except (subprocess.SubprocessError, OSError, FileNotFoundError):
            continue

    # No pager worked, fall back to normal print
    print(text)


def show_filtered_argument_or_group_from_help(parser: argparse.ArgumentParser,
                                              subcommand_name: list[str]):

    # Only handle --help=<keyword> for the current subcommand.
    # Since subparser_init() runs for all subcommands during CLI setup,
    # we skip processing if the subcommand name is not in sys.argv.
    # sys.argv[0] is the program name. The subcommand follows.
    # e.g., for `vllm bench latency`,
    # sys.argv is `['vllm', 'bench', 'latency', ...]`
    # and subcommand_name is "bench latency".
    if len(sys.argv) <= len(subcommand_name) or sys.argv[
            1:1 + len(subcommand_name)] != subcommand_name:
        return

    for arg in sys.argv:
        if arg.startswith('--help='):
            search_keyword = arg.split('=', 1)[1]

            # Enable paged view for full help
            if search_keyword == 'page':
                help_text = parser.format_help()
                _output_with_pager(help_text)
                sys.exit(0)

            # List available groups
            if search_keyword == 'listgroup':
                output_lines = ["\nAvailable argument groups:"]
                for group in parser._action_groups:
                    if group.title and not group.title.startswith(
                            "positional arguments"):
                        output_lines.append(f"  - {group.title}")
                        if group.description:
                            output_lines.append("    " +
                                                group.description.strip())
                        output_lines.append("")
                _output_with_pager("\n".join(output_lines))
                sys.exit(0)

            # For group search
            formatter = parser._get_formatter()
            for group in parser._action_groups:
                if group.title and group.title.lower() == search_keyword.lower(
                ):
                    formatter.start_section(group.title)
                    formatter.add_text(group.description)
                    formatter.add_arguments(group._group_actions)
                    formatter.end_section()
                    _output_with_pager(formatter.format_help())
                    sys.exit(0)

            # For single arg
            matched_actions = []

            for group in parser._action_groups:
                for action in group._group_actions:
                    # search option name
                    if any(search_keyword.lower() in opt.lower()
                           for opt in action.option_strings):
                        matched_actions.append(action)

            if matched_actions:
                header = f"\nParameters matching '{search_keyword}':\n"
                formatter = parser._get_formatter()
                formatter.add_arguments(matched_actions)
                _output_with_pager(header + formatter.format_help())
                sys.exit(0)

            print(f"\nNo group or parameter matching '{search_keyword}'")
            print("Tip: use `--help=listgroup` to view all groups.")
            sys.exit(1)


def get_max_tokens(max_model_len: int, request: Union[ChatCompletionRequest,
                                                      CompletionRequest],
                   input_length: int, default_sampling_params: dict) -> int:

    max_tokens = getattr(request, "max_completion_tokens",
                         None) or request.max_tokens
    default_max_tokens = max_model_len - input_length
    max_output_tokens = current_platform.get_max_output_tokens(input_length)

    return min(val
               for val in (default_max_tokens, max_tokens, max_output_tokens,
                           default_sampling_params.get("max_tokens"))
               if val is not None)
