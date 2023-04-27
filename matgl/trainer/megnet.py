"""
MEGNet Trainer
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from timeit import default_timer

import torch
import torch.nn as nn
from tqdm import tqdm

from matgl.models.megnet import MEGNet

logger = logging.getLogger("megnet_trainer")


def train_one_step(
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    loss_function: nn.Module,
    data_std: torch.Tensor,
    data_mean: torch.Tensor,
    dataloader,
):
    model.train()

    avg_loss = torch.zeros(1)

    start = default_timer()

    for g, labels, attrs in tqdm(dataloader):
        optimizer.zero_grad()

        node_feat = g.ndata["node_type"]
        edge_feat = g.edata["edge_attr"]

        pred = model(g, edge_feat.float(), node_feat.long(), attrs)

        pred = torch.squeeze(pred)

        loss = loss_function(pred, (labels - data_mean) / data_std)

        loss.backward()
        optimizer.step()

        avg_loss += loss.detach()

    stop = default_timer()

    avg_loss = avg_loss.cpu().item() / len(dataloader)
    epoch_time = stop - start

    return avg_loss, epoch_time


def validate_one_step(
    model: nn.Module,
    loss_function: nn.Module,
    data_std: torch.Tensor,
    data_mean: torch.Tensor,
    dataloader: tuple,
):
    avg_loss = torch.zeros(1)

    start = default_timer()

    with torch.no_grad():
        for g, labels, attrs in dataloader:
            node_feat = g.ndata["node_type"]
            edge_feat = g.edata["edge_attr"]

            pred = model(g, edge_feat.float(), node_feat.long(), attrs)

            pred = torch.squeeze(pred)

            loss = loss_function(data_mean + pred * data_std, labels)

            avg_loss += loss

    stop = default_timer()

    avg_loss = avg_loss.cpu().item() / len(dataloader)
    epoch_time = stop - start

    return avg_loss, epoch_time


class StreamingJSONWriter:
    """
    Serialize streaming data to JSON.

    This class holds onto an open file reference to which it carefully
    appends new JSON data. Individual entries are input in a list, and
    after every entry the list is closed so that it remains valid JSON.
    When a new item is added, the file cursor is moved backwards to overwrite
    the list closing bracket.
    """

    def __init__(self, filename, encoder=json.JSONEncoder):
        if os.path.exists(filename):
            self.file = open(filename, "r+")
            self.delimeter = ","
        else:
            self.file = open(filename, "w")
            self.delimeter = "["
        self.encoder = encoder

    def dump(self, obj):
        """
        Dump a JSON-serializable object to file.
        """
        data = json.dumps(obj, cls=self.encoder)
        close_str = "\n]\n"
        self.file.seek(max(self.file.seek(0, os.SEEK_END) - len(close_str), 0))
        self.file.write(f"{self.delimeter}\n    {data}{close_str}")
        self.file.flush()
        self.delimeter = ","

    def close(self):
        self.file.close()


class MEGNetTrainer:
    def __init__(self, model: MEGNet, optimizer: torch.optim.Optimizer, scheduler: torch.optim.lr_scheduler) -> None:
        """
        Parameters:
        model: MEGNet model
        optimizer: torch Optimizer
        """
        self.model = model
        self.optimizer = optimizer
        self.scheduler = scheduler

    def train(
        self,
        num_epochs: int,
        train_loss_func: nn.Module,
        val_loss_func: nn.Module,
        data_std: torch.tensor,
        data_mean: torch.tensor,
        train_loader: tuple,
        val_loader: tuple,
        logger_name: str,
    ) -> None:
        path = os.getcwd()
        # Set a path for best model and checkpoints
        outpath = os.path.join(path, "BestModel")
        checkpath = os.path.join(path, "CheckPoints")
        if os.path.exists(outpath):
            shutil.rmtree(outpath)
        os.mkdir(outpath)
        if os.path.exists(checkpath):
            shutil.rmtree(checkpath)
        os.mkdir(checkpath)
        jsonlog = StreamingJSONWriter(filename=logger_name)
        logger.info("## Training started ##")
        best_val_loss = 1000.0
        for epoch in tqdm(range(num_epochs)):
            train_loss, train_time = train_one_step(
                self.model,
                self.optimizer,
                train_loss_func,
                data_std,
                data_mean,
                train_loader,
            )
            val_loss, val_time = validate_one_step(self.model, val_loss_func, data_std, data_mean, val_loader)

            self.scheduler.step()
            logger.info(
                f"Epoch: {epoch + 1:03} Train Loss: {train_loss:.4f} "
                f"Val Loss: {val_loss:.4f} Train Time: {train_time:.2f} s. "
                f"Val Time: {val_time:.2f} s."
            )
            if val_loss < best_val_loss:
                torch.save(
                    {
                        "epoch": epoch + 1,
                        "model": self.model.as_dict(),
                        "optimizer_state_dict": self.optimizer.state_dict(),
                        "scheduler_state_dict": self.scheduler.state_dict(),
                        "loss": val_loss,
                    },
                    checkpath + "/%05d" % (epoch + 1) + "-%6.5f" % (val_loss) + ".pt",
                )

                log_dict = {
                    "Epoch": epoch + 1,
                    "train_loss": train_loss,
                    "val_loss": val_loss,
                    "train_time": train_time,
                    "val_time": val_time,
                }

                jsonlog.dump(log_dict)
                best_val_loss = val_loss
                torch.save({"model": self.model.as_dict()}, outpath + "/best-model.pt")
        jsonlog.close()
        logger.info("## Training finished ##")
