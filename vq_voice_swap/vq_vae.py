from abc import abstractmethod
import math
import os
from typing import Any, Dict, Optional
from vq_voice_swap.unet import UNetPredictor

import torch
import torch.nn as nn

from .diffusion import Diffusion
from .model import Predictor, TimeEmbedding, WaveGradEncoder, WaveGradPredictor
from .schedule import ExpSchedule
from .util import atomic_save
from .vq import VQ, VQLoss


class VQVAE(nn.Module):
    """
    Abstract base class for a class-conditional waveform VQ-VAE.
    """

    def __init__(
        self,
        encoder: nn.Module,
        vq: VQ,
        vq_loss: VQLoss,
        diffusion: Diffusion,
        num_labels: int,
    ):
        super().__init__()
        self.encoder = encoder
        self.vq = vq
        self.vq_loss = vq_loss
        self.diffusion = diffusion
        self.num_labels = num_labels

    def losses(self, inputs: torch.Tensor, labels: torch.Tensor, **extra_kwargs: Any):
        """
        Compute losses for training the VQVAE.

        :param inputs: the input [N x 1 x T] audio Tensor.
        :param labels: an [N] Tensor of integer labels.
        :return: a dict containing the following keys:
                 - "vq_loss": loss for the vector quantization layer.
                 - "mse": mean loss for all the predictors.
                 - "ts": a 1-D float tensor of the timesteps per batch entry.
                 - "mses": a 1-D tensor of the mean MSE losses per batch entry
                           for all of the predictors.
                 - "mses_dict": a map of separate 1-D MSE tensors for each
                                predictor.
        """
        encoder_out = self.encoder(inputs, **extra_kwargs)
        vq_out = self.vq(encoder_out)
        vq_loss = self.vq_loss(encoder_out, vq_out["embedded"])

        ts = torch.rand(inputs.shape[0]).to(inputs)
        epsilon = torch.randn_like(inputs)
        noised_inputs = self.diffusion.sample_q(inputs, ts, epsilon=epsilon)
        named_preds = self.predictions(
            noised_inputs, ts, cond=vq_out["passthrough"], labels=labels, **extra_kwargs
        )
        mses_dict = {
            name: ((predictions - epsilon) ** 2).flatten(1).mean(1)
            for name, predictions in named_preds.items()
        }
        mses = torch.mean(torch.stack(list(mses_dict.values()), axis=0), axis=0)
        mse = mses.mean()

        return {
            "vq_loss": vq_loss,
            "mse": mse,
            "ts": ts,
            "mses": mses,
            "mses_dict": mses_dict,
        }

    def encode(self, inputs: torch.Tensor) -> torch.Tensor:
        """
        Encode a waveform as discrete symbols.

        :param inputs: an [N x 1 x T] audio Tensor.
        :return: an [N x T1] Tensor of latent codes.
        """
        with torch.no_grad():
            return self.vq(self.encoder(inputs))["idxs"]

    def decode(
        self,
        codes: torch.Tensor,
        labels: torch.Tensor,
        steps: int = 100,
        progress: bool = False,
        key: str = "cond",
    ) -> torch.Tensor:
        """
        Sample the decoder using encoded audio and corresponding labels.

        :param codes: an [N x T1] Tensor of latent codes.
        :param labels: an [N] Tensor of integer labels.
        :param steps: number of diffusion steps.
        :param progress: if True, show a progress bar with tqdm.
        :param key: the key from predictions() to use as a predictor.
        :return: an [N x 1 x T] Tensor of audio.
        """
        cond_seq = self.vq.embed(codes)
        x_T = torch.randn(
            codes.shape[0], 1, codes.shape[1] * self.downsample_rate()
        ).to(codes.device)
        return self.diffusion.ddpm_sample(
            x_T,
            lambda xs, ts, **kwargs: self.predictions(
                xs, ts, cond=cond_seq, labels=labels, **kwargs
            )["cond"],
            steps=steps,
            progress=progress,
        )

    @abstractmethod
    def predictions(
        self, xs: torch.Tensor, ts: torch.Tensor, **kwargs
    ) -> Dict[str, torch.Tensor]:
        """
        Get epsilon predictions from one or more predictors.

        Each predictor has a name in the resulting dict.
        """

    @abstractmethod
    def downsample_rate(self):
        """
        Get the number of audio samples per latent code.
        """


class ConcreteVQVAE(VQVAE):
    def __init__(self, pred_name: str, num_labels: int, base_channels: int = 32):
        encoder = WaveGradEncoder(base_channels=base_channels)
        super().__init__(
            encoder=encoder,
            vq=VQ(encoder.cond_channels, 512),
            vq_loss=VQLoss(),
            diffusion=Diffusion(ExpSchedule()),
            num_labels=num_labels,
        )
        self.pred_name = pred_name
        self.base_channels = base_channels
        self.cond_predictor = make_predictor(
            pred_name,
            base_channels=base_channels,
            cond_channels=encoder.cond_channels,
            num_labels=num_labels,
        )

    def predictions(
        self, xs: torch.Tensor, ts: torch.Tensor, **kwargs
    ) -> Dict[str, torch.Tensor]:
        return dict(cond=self.cond_predictor(xs, ts, **kwargs))

    def save(self, path):
        """
        Save this model, as well as everything needed to construct it, to a
        file.
        """
        state = {
            "kwargs": {
                "pred_name": self.pred_name,
                "num_labels": self.num_labels,
                "base_channels": self.base_channels,
            },
            "state_dict": self.state_dict(),
        }
        atomic_save(state, path)

    @classmethod
    def load(cls, path):
        """
        Load a fresh model instance from a file.
        """
        state = torch.load(path, map_location="cpu")
        obj = cls(**state["kwargs"])
        obj.load_state_dict(state["state_dict"])
        return obj

    def downsample_rate(self):
        return predictor_downsample_rate(self.pred_name)


class CascadeVQVAE(ConcreteVQVAE):
    def __init__(self, pred_name: str, num_labels: int, base_channels: int = 32):
        super().__init__(
            pred_name=pred_name,
            num_labels=num_labels,
            base_channels=base_channels,
        )
        self.scale_cond = TimeScale()
        self.scale_label = TimeScale()
        self.base_predictor = make_predictor(pred_name, base_channels=base_channels)
        self.label_predictor = make_predictor(
            pred_name,
            base_channels=base_channels,
            num_labels=num_labels,
        )

    def predictions(
        self,
        xs: torch.Tensor,
        ts: torch.Tensor,
        cond: Optional[torch.Tensor] = None,
        labels: Optional[torch.Tensor] = None,
        **kwargs,
    ) -> Dict[str, torch.Tensor]:
        base = self.base_predictor(xs, ts, **kwargs)
        label = base.detach() + self.scale_label(
            self.label_predictor(xs, ts, labels=labels, **kwargs),
            ts,
        )
        cond = label.detach() + self.scale_cond(
            self.cond_predictor(xs, ts, cond=cond, labels=labels, **kwargs),
            ts,
        )
        return dict(base=base, label=label, cond=cond)


class TimeScale(nn.Module):
    def __init__(self, channels: int = 256, initial: float = 0.05, scale: float = 3.0):
        super().__init__()
        self.channels = channels
        self.initial = initial
        self.scale = scale
        self.time_emb = TimeEmbedding(channels)
        self.out = nn.Sequential(
            nn.GELU(),
            nn.Linear(channels, 1),
        )
        with torch.no_grad():
            self.out[1].weight.mul_(0)
            self.out[1].bias.fill_(math.log(initial) / scale)

    def forward(self, x: torch.Tensor, t: torch.Tensor):
        scales = self.out(self.time_emb(t) * self.scale).exp()
        while len(scales.shape) < len(x.shape):
            scales = scales[..., None]
        return x * scales


def make_predictor(
    pred_name: str,
    base_channels: int = 32,
    cond_channels: Optional[int] = None,
    **kwargs,
) -> Predictor:
    if pred_name == "wavegrad":
        cond_mult = cond_channels // base_channels if cond_channels else 16
        return WaveGradPredictor(
            base_channels=base_channels, cond_mult=cond_mult, **kwargs
        )
    elif pred_name == "unet":
        return UNetPredictor(
            base_channels=base_channels,
            cond_channels=cond_channels,
            **kwargs,
        )
    else:
        raise ValueError(f"unknown predictor: {pred_name}")


def predictor_downsample_rate(pred_name: str) -> int:
    if pred_name == "wavegrad":
        return 2 ** 6
    elif pred_name == "unet":
        return 2 ** 8
    else:
        raise ValueError(f"unknown predictor: {pred_name}")
