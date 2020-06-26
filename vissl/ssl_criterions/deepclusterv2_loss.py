#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#
import logging
import math
import pprint

import torch
import torch.distributed as dist
from classy_vision.generic.distributed_util import (
    all_reduce_sum,
    gather_from_all,
    get_rank,
    get_world_size,
)
from classy_vision.losses import ClassyLoss, register_loss
from torch import nn
from vissl.utils.misc import get_indices_sparse


@register_loss("deepclusterv2_loss")
class DeepClusterV2Loss(ClassyLoss):
    """
    """

    def __init__(self, config):
        super().__init__()

        size_dataset = config["NUM_TRAIN_SAMPLES"]
        size_memory_per_process = int(math.ceil(size_dataset * 1.0 / get_world_size()))
        self.loss_config = config.DEEPCLUSTERV2_LOSS
        if self.loss_config.DROP_LAST:
            size_memory_per_process -= (
                size_memory_per_process % self.loss_config.BATCHSIZE_PER_REPLICA
            )

        self.nmb_mbs = len(self.loss_config.MEMORY_PARAMS.CROPS_FOR_MB)
        self.nmb_heads = len(self.loss_config.NMB_CLUSTERS)
        self.nmb_clusters = self.loss_config.NMB_CLUSTERS
        self.embedding_dim = self.loss_config.MEMORY_PARAMS.EMBEDDING_DIM
        self.crops_for_mb = self.loss_config.MEMORY_PARAMS.CROPS_FOR_MB
        self.nmb_unique_idx = self.loss_config.BATCHSIZE_PER_REPLICA
        self.nmb_crops = self.loss_config.NMB_CROPS
        self.temperature = self.loss_config.TEMPERATURE
        self.nmb_kmeans_iters = self.loss_config.KMEANS_ITERS
        self.start_idx = 0

        self.register_buffer(
            "local_memory_embeddings",
            torch.zeros(self.nmb_mbs, size_memory_per_process, self.embedding_dim),
        )
        self.register_buffer(
            "local_memory_index", torch.zeros(size_memory_per_process).long()
        )
        self.register_buffer(
            "assignments", -100 * torch.ones(self.nmb_heads, size_dataset).long()
        )
        for i, k in enumerate(self.loss_config.NMB_CLUSTERS):
            self.register_buffer(
                "centroids" + str(i), torch.rand(k, self.embedding_dim)
            )

        self.criterion = nn.CrossEntropyLoss(ignore_index=-100)

    @classmethod
    def from_config(cls, config):
        return cls(config)

    def forward(self, output, idx):
        assert len(output) == 1
        output = output[0]
        output = nn.functional.normalize(output, dim=1, p=2)

        loss = 0
        for i in range(self.nmb_heads):
            scores = (
                torch.mm(output, getattr(self, "centroids" + str(i)).t())
                / self.temperature
            )
            loss += self.criterion(scores, self.assignments[i][idx])
        loss /= self.nmb_heads

        self.update_memory_bank(output, idx)

        return loss

    def init_memory(self, dataloader, model):
        logging.info(f"Rank: {get_rank()}, Start initializing memory banks")
        start_idx = 0
        with torch.no_grad():
            for inputs in dataloader:
                nmb_unique_idx = len(inputs["data_idx"][0]) // self.nmb_crops
                index = inputs["data_idx"][0][:nmb_unique_idx].cuda(non_blocking=True)

                # get embeddings
                outputs = []
                for crop_idx in self.crops_for_mb:
                    inp = inputs["data"][0][crop_idx].cuda(non_blocking=True)
                    outputs.append(nn.functional.normalize(model(inp)[0], dim=1, p=2))

                # fill the memory bank
                self.local_memory_index[start_idx : start_idx + nmb_unique_idx] = index
                for mb_idx, embeddings in enumerate(outputs):
                    self.local_memory_embeddings[mb_idx][
                        start_idx : start_idx + nmb_unique_idx
                    ] = embeddings
                start_idx += nmb_unique_idx
        logging.info(
            f"Rank: {get_rank()}, Memory banks initialized: "
            "full first forward pass done"
        )

    def update_memory_bank(self, emb, idx):
        nmb_unique_idx = len(idx) // self.nmb_crops
        idx = idx[:nmb_unique_idx]
        self.local_memory_index[self.start_idx : self.start_idx + nmb_unique_idx] = idx
        for i, crop_idx in enumerate(self.crops_for_mb):
            self.local_memory_embeddings[i][
                self.start_idx : self.start_idx + nmb_unique_idx
            ] = emb[crop_idx * nmb_unique_idx : (crop_idx + 1) * nmb_unique_idx]
        self.start_idx += nmb_unique_idx

    def cluster_memory(self):
        self.start_idx = 0
        j = 0
        with torch.no_grad():
            for i_K, K in enumerate(self.nmb_clusters):
                # run distributed k-means

                # init centroids with elements from memory bank of rank 0
                centroids = torch.empty(K, self.embedding_dim).cuda(non_blocking=True)
                if get_rank() == 0:
                    random_idx = torch.randperm(len(self.local_memory_embeddings[j]))[
                        :K
                    ]
                    assert len(random_idx) >= K, "please reduce the number of centroids"
                    centroids = self.local_memory_embeddings[j][random_idx]
                dist.broadcast(centroids, 0)

                for n_iter in range(self.nmb_kmeans_iters + 1):

                    # E step
                    dot_products = torch.mm(
                        self.local_memory_embeddings[j], centroids.t()
                    )
                    _, assignments = dot_products.max(dim=1)

                    # finish
                    if n_iter == self.nmb_kmeans_iters:
                        break

                    # M step
                    where_helper = get_indices_sparse(assignments.cpu().numpy())
                    counts = torch.zeros(K).cuda(non_blocking=True).int()
                    emb_sums = torch.zeros(K, self.embedding_dim).cuda(
                        non_blocking=True
                    )
                    for k in range(len(where_helper)):
                        if len(where_helper[k][0]) > 0:
                            emb_sums[k] = torch.sum(
                                self.local_memory_embeddings[j][where_helper[k][0]],
                                dim=0,
                            )
                            counts[k] = len(where_helper[k][0])
                    all_reduce_sum(counts)
                    mask = counts > 0
                    all_reduce_sum(emb_sums)
                    centroids[mask] = emb_sums[mask] / counts[mask].unsqueeze(1)

                    # normalize centroids
                    centroids = nn.functional.normalize(centroids, dim=1, p=2)

                getattr(self, "centroids" + str(i_K)).copy_(centroids)
                # gather the assignments
                assignments_all = gather_from_all(assignments)
                indexes_all = gather_from_all(self.local_memory_index)
                self.assignments[i_K] = -100
                self.assignments[i_K][indexes_all] = assignments_all

                j = (j + 1) % self.nmb_mbs

        logging.info(f"Rank: {get_rank()}, clustering of the memory bank done")

    def __repr__(self):
        repr_dict = {"name": self._get_name()}
        return pprint.pformat(repr_dict, indent=2)
