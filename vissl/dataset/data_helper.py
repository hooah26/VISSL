# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

import logging
import queue

import numpy as np
import torch
from PIL import Image
from torch.utils.data import Dataset
from torch.utils.data.distributed import DistributedSampler


def get_mean_image(crop_size):
    img = Image.fromarray(128 * np.ones((crop_size, crop_size, 3), dtype=np.uint8))
    return img


class StatefulDistributedSampler(DistributedSampler):
    def __init__(self, dataset, batch_size=None):
        super().__init__(dataset, shuffle=False)

        self.start_iter = 0
        self.batch_size = batch_size
        self.total_size = len(dataset) - (len(dataset) % self.num_replicas)
        self.num_samples = self.total_size // self.num_replicas
        logging.info(f"rank: {self.rank}: Sampler created...")

    def __iter__(self):
        # partition data into num_replicas and optionally shuffle within a rank
        g = torch.Generator()
        g.manual_seed(self.epoch)
        shuffling = torch.randperm(self.num_samples, generator=g).tolist()
        indices = np.array(
            list(
                range(
                    (self.rank * self.num_samples), (self.rank + 1) * self.num_samples
                )
            )
        )[shuffling].tolist()

        # make sure we have correct number of samples per replica
        assert len(indices) == self.num_samples
        assert self.batch_size > 0, "batch_size not set for the sampler"

        # resume the sampler
        indices = indices[(self.start_iter * self.batch_size) :]
        return iter(indices)

    def set_start_iter(self, start_iter):
        self.start_iter = start_iter


class QueueDataset(Dataset):
    """
    This class helps dealing with the invalid images in the dataset by using
    two queue. One queue is used to enqueue seen and valid images from previous
    batches. The other queue is used to dequeue. The class is implemented such
    that the same batch will never have duplicate images. If we can't dequeue a
    valid image, we return None for that instance.

    Args:
        queue_size: size the the queue (ideally set it to batch_size). Both queues
                    will be of the same size
    """

    def __init__(self, queue_size):

        self.queue_size = queue_size
        # we create a CPU queue to buffer the valid seen images. We use these
        # images to replace the invalid images in the minibatch
        # 2 queues (FIFO) per gpu of size = batch size per gpu (64 img):
        #   a) 1st queue is used only to dequeue seen images. We get images from
        #      this queue only to backfill.
        #   b) 2nd queue is used only to add the new incoming valid seen images
        self.queue_init = False
        self.dequeue_images_queue = None
        self.enqueue_images_queue = None

    def _init_queues(self):
        self.dequeue_images_queue = queue.Queue(maxsize=self.queue_size)
        self.enqueue_images_queue = queue.Queue(maxsize=self.queue_size)
        self.queue_init = True
        logging.info(f"QueueDataset enabled. Using queue_size: {self.queue_size}")

    def _refill_dequeue_buffer(self):
        dequeue_qsize = self._get_dequeue_buffer_size()
        for _ in range(self.queue_size - dequeue_qsize):
            try:
                self.dequeue_images_queue.put(
                    self.enqueue_images_queue.get(), block=True
                )
            except Exception:
                continue

    def _enqueue_valid_image(self, img):
        try:
            self.enqueue_images_queue.put(img, block=True, timeout=0.1)
            return
        except queue.Full:
            return

    def _dequeue_valid_image(self):
        try:
            return self.dequeue_images_queue.get(block=True, timeout=0.1)
        except queue.Empty:
            return None

    def _get_enqueue_buffer_size(self):
        return self.enqueue_images_queue.qsize()

    def _get_dequeue_buffer_size(self):
        return self.dequeue_images_queue.qsize()

    def on_sucess(self, sample):
        self._enqueue_valid_image(sample)
        if self.enqueue_images_queue.full() and not self.dequeue_images_queue.full():
            self._refill_dequeue_buffer()

    def on_failure(self):
        sample, is_success = None, False
        if self._get_dequeue_buffer_size() > 0:
            sample = self._dequeue_valid_image()
            if sample is not None:
                is_success = True
        return sample, is_success
