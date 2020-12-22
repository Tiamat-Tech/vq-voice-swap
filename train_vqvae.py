"""
Train an unconditional diffusion model on waveforms.
"""

import argparse
import os

import torch
from torch.optim import AdamW

from vq_voice_swap.dataset import create_data_loader
from vq_voice_swap.vq_vae import WaveGradVQVAE


def main():
    args = arg_parser().parse_args()

    data_loader, num_labels = create_data_loader(
        directory=args.data_dir, batch_size=args.batch_size
    )

    if os.path.exists(args.checkpoint_path):
        print("loading from checkpoint...")
        model = WaveGradVQVAE.load(args.checkpoint_path)
        assert model.num_labels == num_labels
    else:
        print("creating new model...")
        model = WaveGradVQVAE(num_labels)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)

    opt = AdamW(model.parameters(), lr=args.lr)

    for i, data_batch in enumerate(repeat_dataset(data_loader)):
        audio_seq = data_batch["samples"][:, None].to(device)
        labels = data_batch["label"].to(device)
        losses = model.losses(audio_seq, labels)
        loss = losses["vq_loss"] + losses["mse"]

        opt.zero_grad()
        loss.backward()
        opt.step()

        step = i + 1
        print(
            f"step {step}: vq_loss={losses['vq_loss'].item()} mse={losses['mse'].item()}"
        )
        if step % args.save_interval == 0:
            model.save(args.checkpoint_path)


def repeat_dataset(data_loader):
    while True:
        yield from data_loader


def arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--lr", default=1e-4, type=float)
    parser.add_argument("--batch-size", default=8, type=int)
    parser.add_argument("--checkpoint-path", default="model_vqvae.pt", type=str)
    parser.add_argument("--save-interval", default=500, type=int)
    parser.add_argument("data_dir", type=str)
    return parser


if __name__ == "__main__":
    main()
