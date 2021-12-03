# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
# Copyright (c) 2018, NVIDIA CORPORATION. All rights reserved.
import datetime
import logging
import time

import torch
import torch.distributed as dist

from maskrcnn_benchmark.utils.comm import get_world_size, is_main_process
from maskrcnn_benchmark.utils.metric_logger import MetricLogger

def reduce_loss_dict(loss_dict):
    """
    Reduce the loss dictionary from all processes so that process with rank
    0 has the averaged results. Returns a dict with the same fields as
    loss_dict, after reduction.
    """
    world_size = get_world_size()
    if world_size < 2:
        return loss_dict
    with torch.no_grad():
        loss_names = []
        all_losses = []
        for k in sorted(loss_dict.keys()):
            loss_names.append(k)
            all_losses.append(loss_dict[k])
        all_losses = torch.stack(all_losses, dim=0)
        dist.reduce(all_losses, dst=0)
        if dist.get_rank() == 0:
            # only main process gets accumulated, so only divide by
            # world_size in this case
            all_losses /= world_size
        reduced_losses = {k: v for k, v in zip(loss_names, all_losses)}
    return reduced_losses


def do_train(
    model,
    data_loader,
    optimizer,
    scheduler,
    checkpointer,
    device,
    checkpoint_period,
    arguments,
    use_amp,
    cfg,
    dllogger,
    per_iter_end_callback_fn=None,
):
    dllogger.log(step="PARAMETER", data={"train_start": True})
    logger = logging.getLogger("maskrcnn_benchmark.trainer")
    logger.info("Start training")
    meters = MetricLogger(delimiter="  ")
    max_iter = len(data_loader)
    print("max_iter: ", max_iter)
    start_iter = arguments["iteration"]
    model.train()
    start_training_time = time.time()
    end = time.time()

    if use_amp:
        scaler = torch.cuda.amp.GradScaler(init_scale=8192.0)
    for iteration, (images, targets, _) in enumerate(data_loader, start_iter):
        data_time = time.time() - end
        iteration = iteration + 1
        arguments["iteration"] = iteration

        images = images.to(device)
        targets = [target.to(device) for target in targets]

        if use_amp:
            with torch.cuda.amp.autocast():
                loss_dict = model(images, targets)
        else:
            loss_dict = model(images, targets)

        losses = sum(loss for loss in loss_dict.values())

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = reduce_loss_dict(loss_dict)
        losses_reduced = sum(loss for loss in loss_dict_reduced.values())
        meters.update(loss=losses_reduced, **loss_dict_reduced)


        # Note: If mixed precision is not used, this ends up doing nothing
        # Otherwise apply loss scaling for mixed-precision recipe
        if use_amp:
            scaler.scale(losses).backward()
        else:
            losses.backward()

        def _take_step():
            if use_amp:
                scaler.step(optimizer)
                scaler.update()
            else:
                optimizer.step()
            scheduler.step()
            optimizer.zero_grad()
        
        if not cfg.SOLVER.ACCUMULATE_GRAD:
            _take_step()
        else:
            if (iteration + 1) % cfg.SOLVER.ACCUMULATE_STEPS == 0:
                for param in model.parameters():
                    if param.grad is not None:
                        param.grad.data.div_(cfg.SOLVER.ACCUMULATE_STEPS)
                _take_step()

        batch_time = time.time() - end
        end = time.time()

        if iteration % 5 == 0 and is_main_process():
            logger.info("iter: %d batch_time: %f" % (iteration, batch_time))

        if(iteration > 500):
            meters.update(time=batch_time, data=data_time)

            eta_seconds = meters.time.global_avg * (max_iter - iteration)
            eta_string = str(datetime.timedelta(seconds=int(eta_seconds)))

            if (iteration % 5 == 0 or iteration == max_iter) and is_main_process():
                logger.info(
                    meters.delimiter.join(
                        [
                            "eta: {eta}",
                            "avg iteration time(s): {avg_iter:.2f}",
                            "avg iter/s: {iter_s:.2f}",
                            "throughput: {speed:.2f} FPS",
                            "iter: {iter}",
                            "{meters}",
                            "lr: {lr:.6f}",
                            "max mem: {memory:.0f}",
                        ]
                    ).format(
                        eta=eta_string,
                        avg_iter=meters.time.global_avg,
                        iter_s=1.0/meters.time.global_avg,
                        speed=1.0/meters.time.global_avg*int(cfg.SOLVER.IMS_PER_BATCH),
                        iter=iteration,
                        meters=str(meters),
                        lr=optimizer.param_groups[0]["lr"],
                        memory=torch.cuda.max_memory_allocated() / 1024.0 / 1024.0,
                    )
                )

        if iteration % checkpoint_period == 0:
            checkpointer.save("model_{:07d}".format(iteration), **arguments)
        if iteration == max_iter:
            checkpointer.save("model_final", **arguments)

        # per-epoch work (testing)
        if per_iter_end_callback_fn is not None:
            early_exit = per_iter_end_callback_fn(iteration=iteration)
            if early_exit:
                break

    if is_main_process():
        total_training_time = time.time() - start_training_time
        total_time_str = str(datetime.timedelta(seconds=total_training_time))
        logger.info("Total training time: {} ".format(total_time_str))
        logger.info("Final Loss at iteration {}: {}".format(max_iter, str(meters)))
