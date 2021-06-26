from typing import Optional

from .base import Encoder, Predictor
from .unet import UNetEncoder, UNetPredictor
from .wavegrad import WaveGradEncoder, WaveGradPredictor


def make_predictor(
    pred_name: str,
    base_channels: int = 32,
    num_labels: Optional[int] = None,
    cond_channels: Optional[int] = None,
    dropout: float = 0.0,
) -> Predictor:
    """
    Create a Predictor model from a human-readable name.
    """
    if pred_name == "wavegrad":
        assert not dropout, "dropout not supported for wavegrad"
        cond_mult = cond_channels // base_channels if cond_channels else 16
        return WaveGradPredictor(
            base_channels=base_channels,
            cond_mult=cond_mult,
            num_labels=num_labels,
        )
    elif pred_name == "unet":
        return UNetPredictor(
            base_channels=base_channels,
            cond_channels=cond_channels,
            num_labels=num_labels,
            dropout=dropout,
        )
    else:
        raise ValueError(f"unknown predictor: {pred_name}")


def make_encoder(
    enc_name: str,
    base_channels: int = 32,
    cond_mult: int = 16,
) -> Encoder:
    """
    Create an Encoder model from a human-readable name.
    """
    if enc_name == "wavegrad":
        return WaveGradEncoder(cond_mult=cond_mult, base_channels=base_channels)
    elif enc_name == "unet":
        return UNetEncoder(
            base_channels=base_channels, out_channels=base_channels * cond_mult
        )
    else:
        raise ValueError(f"unknown encoder: {enc_name}")


def predictor_downsample_rate(pred_name: str) -> int:
    """
    Get the downsample rate of a named Predictor, to ensure that input
    sequences are evenly divisible by it.
    """
    if pred_name == "wavegrad":
        return 2 ** 6
    elif pred_name == "unet":
        return 2 ** 8
    else:
        raise ValueError(f"unknown predictor: {pred_name}")