from pathlib import Path


def checkpoint_info(path: Path) -> dict:
    import torch
    ckpt = torch.load(path, map_location="cpu")
    return {
        "train_steps": ckpt.get("train_steps"),
        "experiences_seen": ckpt.get("experiences_seen"),
        "epsilon": ckpt.get("epsilon"),
        "n_features": ckpt.get("n_features", 5),
    }
