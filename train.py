import os
import copy
import time
import yaml
import torch
import argparse
import matplotlib
import matplotlib.pyplot as plt
import numpy as np

from tensorboardX import SummaryWriter

from ncdssm.torch_utils import grad_norm, prepend_time_zero, torch2numpy
from ncdssm.plotting import show_time_series_forecast
from ncdssm.evaluation import evaluate_simple_ts, evaluate_sporadic
import experiments.utils

from model import build_model
from dataset import get_dataset


def train_step(train_batch, model, optimizer, reg_scheduler, step, device, config):
    batch_target = train_batch["past_target"].to(device)
    batch_times = train_batch["past_times"].to(device)
    batch_mask = train_batch["past_mask"].to(device)
    optimizer.zero_grad()
    out = model(
        batch_target,
        batch_mask,
        batch_times,
        num_samples=config.get("num_samples", 1),
    )
    cond_ll = out["likelihood"]
    reg = out["regularizer"]
    loss = -(cond_ll + reg_scheduler.val * reg).mean(0)
    loss.backward()
    if step <= config.get("ssm_params_warmup_steps", 0):
        ctkf_lr = optimizer.param_groups[0]["lr"]
        optimizer.param_groups[0]["lr"] = 0
    total_grad_norm = grad_norm(model.parameters())
    if total_grad_norm < float("inf"):
        if config["max_grad_norm"] != float("inf"):
            torch.nn.utils.clip_grad_norm_(
                model.parameters(), max_norm=config["max_grad_norm"]
            )
        optimizer.step()
    else:
        print("Skipped gradient update!")
        optimizer.zero_grad()
    if step <= config.get("ssm_params_warmup_steps", 0):
        optimizer.param_groups[0]["lr"] = ctkf_lr
    print(
        f"Step {step}: Loss={loss.item():.4f},"
        f" Grad Norm: {total_grad_norm.item():.2f},"
        f" Reg-Coeff: {reg_scheduler.val:.2f}"
    )
    return dict(
        loss=loss.item(), cond_ll=cond_ll.mean(0).item(), reg=reg.mean(0).item()
    )


def main():
    matplotlib.use("Agg")

    # COMMAND-LINE ARGS
    model_name = "NCDSSMNL"
    sporadic = True
    config = {
        "experiment": "low-dimensional",
        "missing_p": 20,
        "model": model_name,
        "dataset": "climate",
        "data_fold": 0,
        "exp_root_dir": f"./results/{model_name}/{'{timestamp}'}/",
        "train_batch_size": 64,
        "test_batch_size": 112,
        "learning_rate": 0.01,
        "weight_decay": 0.0001,
        "lr_decay_rate": 0.9,
        "lr_decay_steps": 100,
        "ctkf_params_warmup_steps": 0,
        "max_grad_norm": 100,
        "num_steps": 20,
        "log_steps": 10,
        "save_steps": 10,
        "device": "cpu",
        "z_dim": 10,
        "y_dim": 4,
        "u_dim": 0,
        "aux_dim": 4,
        "K": 10,
        "concat_mask": False,
        "emission_mlp_units": 32,
        "emission_hidden_layers": 1,
        "emission_nonlinearity": "softplus",
        "emission_tied_cov": True,
        "emission_trainable_cov": False,
        "inference_mlp_units": 32,
        "inference_nonlinearity": "softplus",
        "inference_tied_cov": False,
        "inference_trainable_cov": False,
        "drift_mlp_units": 32,
        "drift_hidden_layers": 1,
        "drift_nonlinearity": "softplus",
        "drift_last_nonlinearity": False,
        "drift_zero_init_last": False,
        "drift_spectral_norm": True,
        "fixed_diffusion": True,
        "diffusion_mlp_units": 32,
        "diffusion_hidden_layers": 1,
        "diffusion_nonlinearity": "softplus",
        "diffusion_spectral_norm": True,
        "fixed_H": False,
        "num_forecast": 50,
        "num_plots": 5,
        "integration_step_size": 0.1,
        "integration_method": "euler",
        "log_dir": f"./logs/{model_name}/{time.time()}",
        "ckpt_dir": "checkpoints",
    }

    os.makedirs("checkpoints", exist_ok=True)

    if sporadic:
        evaluate_fn = evaluate_sporadic
    else:
        evaluate_fn = evaluate_simple_ts

    # DATA
    train_dataset, val_dataset, _ = get_dataset(config)
    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=config["train_batch_size"],
        num_workers=4,  # NOTE: 0 may be faster for climate dataset
        shuffle=True,
        collate_fn=train_dataset.collate_fn,
    )
    val_loader = torch.utils.data.DataLoader(
        val_dataset,
        batch_size=config["test_batch_size"],
        collate_fn=val_dataset.collate_fn,
    )
    train_gen = iter(train_loader)
    # test_gen = iter(test_loader)

    # MODEL
    device = torch.device(config["device"])
    model = build_model(config=config)
    kf_param_names = {
        name for name, _ in model.named_parameters() if "base_ssm" in name
    }
    kf_params = [
        param for name, param in model.named_parameters() if name in kf_param_names
    ]
    non_kf_params = [
        param for name, param in model.named_parameters() if name not in kf_param_names
    ]
    print(kf_param_names)
    optim = torch.optim.Adam(
        params=[
            {"params": kf_params},
            {"params": non_kf_params},
        ],
        lr=config["learning_rate"],
        weight_decay=config.get("weight_decay", 0.0),
    )
    lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(
        optim, gamma=config["lr_decay_rate"]
    )
    reg_scheduler = experiments.utils.LinearScheduler(
        iters=config.get("reg_anneal_iters", 0),
        maxval=config.get("reg_coeff_maxval", 1.0),
    )
    start_step = 1
    model = model.to(device)
    num_params = 0
    for name, param in model.named_parameters():
        num_params += np.prod(param.size())
        print(name, param.size())
    print(f"Total Paramaters: {num_params.item()}")

    # TRAIN & EVALUATE
    num_steps = config["num_steps"]
    log_steps = config["log_steps"]
    save_steps = config["save_steps"]
    log_dir = config["log_dir"]
    writer = SummaryWriter(logdir=log_dir)

    with open(os.path.join(log_dir, "config.yaml"), "w") as fp:
        yaml.dump(config, fp, default_flow_style=False, sort_keys=False)

    for step in range(start_step, num_steps + 1):
        try:
            train_batch = next(train_gen)
        except StopIteration:
            train_gen = iter(train_loader)
            train_batch = next(train_gen)
        train_result = train_step(
            train_batch, model, optim, reg_scheduler, step, device, config
        )
        summary_items = copy.deepcopy(train_result)

        if step % config["lr_decay_steps"] == 0:
            lr_scheduler.step()

        if step % config.get("reg_anneal_every", 1) == 0:
            reg_scheduler.step()

        if step % save_steps == 0 or step == num_steps:
            model_path = os.path.join(config["ckpt_dir"], f"model_{step}.pt")
            torch.save(
                {
                    "step": step,
                    "model": model.state_dict(),
                    "optimizer": optim.state_dict(),
                    "scheduler": lr_scheduler.state_dict(),
                    "config": config,
                },
                model_path,
            )

        if step % log_steps == 0 or step == num_steps:
            metrics = evaluate_fn(
                val_loader, model, device, num_samples=config["num_forecast"]
            )
            for m, v in metrics.items():
                writer.add_scalar(m, v, global_step=step)
            folder = os.path.join(log_dir, "plots", f"step{step}")
            os.makedirs(folder, exist_ok=True)
            plot_count = 0
            while plot_count < config["num_plots"]:
                for test_batch in val_loader:
                    past_target = test_batch["past_target"].to(device)
                    B, T, D = past_target.shape
                    mask = test_batch["past_mask"].to(device)
                    future_target = test_batch["future_target"].to(device)
                    past_times = test_batch["past_times"].to(device)
                    future_times = test_batch["future_times"].to(device)
                    if past_times[0] > 0:
                        past_times, past_target, mask = prepend_time_zero(
                            past_times, past_target, mask
                        )
                    predict_result = model.forecast(
                        past_target,
                        mask,
                        past_times.view(-1),
                        future_times.view(-1),
                        num_samples=config["num_forecast"],
                    )
                    reconstruction = predict_result["reconstruction"]
                    forecast = predict_result["forecast"]
                    for j in range(B):
                        masked_past_target = past_target.clone()
                        masked_past_target[mask == 0.0] = float("nan")
                        fig = show_time_series_forecast(
                            (12, 5),
                            torch2numpy(past_times),
                            torch2numpy(future_times),
                            torch2numpy(torch.cat([past_target, future_target], 1))[j],
                            torch2numpy(
                                torch.cat([masked_past_target, future_target], 1)
                            )[j],
                            torch2numpy(reconstruction)[:, j],
                            torch2numpy(forecast)[:, j],
                            file_path=os.path.join(folder, f"series_{plot_count}.png"),
                        )
                        plt.close(fig)
                        plot_count += 1

                        if plot_count >= config["num_plots"]:
                            break
                    if plot_count >= config["num_plots"]:
                        break
        for k, v in summary_items.items():
            writer.add_scalar(k, v, global_step=step)
        writer.flush()


if __name__ == "__main__":
    main()
