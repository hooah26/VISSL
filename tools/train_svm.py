# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

import logging
import multiprocessing as mp
import os

import hydra
import numpy as np
from distributed_train import get_hook_generator, launch_distributed
from omegaconf import DictConfig
from vissl.utils.checkpoint import get_absolute_path
from vissl.utils.hydra_config import convert_to_attrdict, is_hydra_available, print_cfg
from vissl.utils.logger import setup_logging
from vissl.utils.misc import merge_features
from vissl.utils.svm_utils.svm_trainer import SVMTrainer


def train_svm(cfg, output_dir, layername):
    # print the coniguration used for svm training
    print_cfg(cfg)

    # train the svm
    logging.info(f"Training SVM for layer: {layername}")
    trainer = SVMTrainer(cfg["SVM"], layer=layername)
    train_data = merge_features(output_dir, "train", layername, cfg)
    train_features, train_targets = train_data["features"], train_data["targets"]
    trainer.train(train_features, train_targets)

    # test the svm
    test_data = merge_features(output_dir, "test", layername, cfg)
    test_features, test_targets = test_data["features"], test_data["targets"]
    trainer.test(test_features, test_targets)
    logging.info("All Done!")


def main(args, config):
    # setup logging
    setup_logging(__name__)

    # print the coniguration used
    print_cfg(config)

    # extract the features
    hook_generator = get_hook_generator(config)
    launch_distributed(config, args, hook_generator=hook_generator)

    # get the layers for which we will train svm
    layers = config.MODEL.EVAL_FEATURES
    output_dir = get_absolute_path(config.SVM.OUTPUT_DIR)

    # train svm for each layer. parallelize it.
    running_tasks = [
        mp.Process(target=train_svm, args=(config, output_dir, layer))
        for layer in layers
    ]
    for running_task in running_tasks:
        running_task.start()
    for running_task in running_tasks:
        running_task.join()

    # collect the mAP stats for all the layers and report
    output_mAP = []
    for layer in layers:
        try:
            ap_file = os.path.join(output_dir, layer, "test_ap.npy")
            output_mAP.append(round(100.0 * np.mean(np.load(ap_file)), 3))
        except Exception:
            output_mAP.append(-1)
    logging.info(f"AP for various layers:\n {layers}: {output_mAP}")


@hydra.main(config_path="hydra_configs", config_name="defaults")
def hydra_main(cfg: DictConfig):
    args, config = convert_to_attrdict(cfg)
    main(args, config)


if __name__ == "__main__":
    assert is_hydra_available(), "Make sure to install hydra"
    hydra_main()
