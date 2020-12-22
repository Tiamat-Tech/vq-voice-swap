from abc import abstractmethod
import os

import torch
import torch.nn as nn

from .diffusion import Diffusion
from .model import Predictor, WaveGradEncoder, WaveGradPredictor
from .schedule import ExpSchedule
from .vq import VQ, VQLoss


class VQVAE(nn.Module):
    """
    Abstract base class for a class-conditional waveform VQ-VAE.
    """

    def __init__(
        self,
        encoder: nn.Module,
        vq: VQ,
        predictor: Predictor,
        vq_loss: VQLoss,
        diffusion: Diffusion,
        num_labels: int,
        embedding_dim: int,
    ):
        super().__init__()
        self.encoder = encoder
        self.vq = vq
        self.predictor = predictor
        self.vq_loss = vq_loss
        self.diffusion = diffusion
        self.num_labels = num_labels
        self.embedding_dim = embedding_dim
        self.label_embeddings = nn.Embedding(num_labels, embedding_dim)

    def losses(self, inputs: torch.Tensor, labels: torch.Tensor):
        """
        Compute losses for training the VQVAE.

        :param inputs: the input [N x 1 x T] audio Tensor.
        :param labels: an [N] Tensor of integer labels.
        :return: a dict containing the following keys:
                 - "vq_loss": loss for the vector quantization layer.
                 - "mse": loss for the diffusion decoder.
        """
        encoder_out = self.encoder(inputs)
        vq_out = self.vq(encoder_out)
        vq_loss = self.vq_loss(inputs, vq_out["embedded"])

        ts = torch.rand(inputs.shape[0]).to(inputs)
        noised_inputs = self.diffusion.sample_q(inputs, ts)
        cond_seq = vq_out["passthrough"] + self.label_embeddings(labels)[..., None]
        predictions = self.predictor(noised_inputs, ts, cond=cond_seq)
        mse = ((predictions - inputs) ** 2).mean()

        return {"vq_loss": vq_loss, "mse": mse}

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
    ) -> torch.Tensor:
        """
        Sample the decoder using encoded audio and corresponding labels.

        :param codes: an [N x T1] Tensor of latent codes.
        :param labels: an [N] Tensor of integer labels.
        :param steps: number of diffusion steps.
        :param progress: if True, show a progress bar with tqdm.
        :return: an [N x 1 x T] Tensor of audio.
        """
        cond_seq = self.vq.embed(codes) + self.label_embeddings(labels)[..., None]
        x_T = torch.randn(
            codes.shape[0], 1, codes.shape[1] * self.downsample_rate()
        ).to(codes.device)
        return self.diffusion.ddpm_sample(
            x_T, self.predictor.condition(cond=cond_seq), steps=steps, progress=progress
        )

    @abstractmethod
    def downsample_rate(self):
        """
        Get the number of audio samples per latent code.
        """


class WaveGradVQVAE(VQVAE):
    def __init__(self, num_labels: int):
        super().__init__(
            encoder=WaveGradEncoder(),
            vq=VQ(512, 512),
            predictor=WaveGradPredictor(),
            vq_loss=VQLoss(),
            diffusion=Diffusion(ExpSchedule()),
            num_labels=num_labels,
            embedding_dim=512,
        )

    def save(self, path):
        """
        Save this model, as well as everything needed to construct it, to a
        file.
        """
        state = {
            "kwargs": {"num_labels": self.num_labels},
            "state_dict": self.state_dict(),
        }
        tmp_path = path + ".tmp"
        torch.save(state, tmp_path)
        os.rename(tmp_path, path)

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
        return 64
