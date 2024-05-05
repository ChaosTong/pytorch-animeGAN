import os
import torch
import cv2
import time
import torch.optim as optim
import numpy as np
from glob import glob
from torch.cuda.amp import GradScaler, autocast
from torch.nn.parallel.distributed import DistributedDataParallel
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm
from utils.image_processing import denormalize_input, preprocess_images, resize_image
from losses import LossSummary, AnimeGanLoss
from utils import load_checkpoint, save_checkpoint, read_image
from utils.common import set_lr
from color_transfer import color_transfer_pytorch


def transfer_color_and_rescale(src, target):
    """Transfer color from src image to target then rescale to [-1, 1]"""
    out = color_transfer_pytorch(src, target)  # [0, 1]
    out = (out / 0.5) - 1
    return out 

def gaussian_noise():
    gaussian_mean = torch.tensor(0.0)
    gaussian_std = torch.tensor(0.1)
    return torch.normal(gaussian_mean, gaussian_std)

def convert_to_readable(seconds):
    return time.strftime('%H:%M:%S', time.gmtime(seconds))


def revert_to_np_image(image_tensor):
    image = image_tensor.cpu().numpy()
    # CHW
    image = image.transpose(1, 2, 0)
    image = denormalize_input(image, dtype=np.int16)
    return image[..., ::-1]  # to RGB


def save_generated_images(images: torch.Tensor, save_dir: str):
    """Save generated images `(*, 3, H, W)` range [-1, 1] into disk"""
    os.makedirs(save_dir, exist_ok=True)
    images = images.clone().detach().cpu().numpy()
    images = images.transpose(0, 2, 3, 1)
    n_images = len(images)

    for i in range(n_images):
        img = images[i]
        img = denormalize_input(img, dtype=np.int16)
        img = img[..., ::-1]
        cv2.imwrite(os.path.join(save_dir, f"G{i}.jpg"), img)


class DDPTrainer:
    def _init_distributed(self):
        if self.cfg.ddp:
            self.logger.info("Setting up DDP")
            self.pg = torch.distributed.init_process_group(
                backend="nccl",
                rank=self.cfg.local_rank,
                world_size=self.cfg.world_size
            )
            self.G = torch.nn.SyncBatchNorm.convert_sync_batchnorm(self.G, self.pg)
            self.D = torch.nn.SyncBatchNorm.convert_sync_batchnorm(self.D, self.pg)
            torch.cuda.set_device(self.cfg.local_rank)
            self.G.cuda(self.cfg.local_rank)
            self.D.cuda(self.cfg.local_rank)
            self.logger.info("Setting up DDP Done")

    def _init_amp(self, enabled=False):
        # self.scaler = torch.cuda.amp.GradScaler(enabled=enabled, growth_interval=100)
        self.scaler_g = GradScaler(enabled=enabled)
        self.scaler_d = GradScaler(enabled=enabled)
        if self.cfg.ddp:
            self.G = DistributedDataParallel(
                self.G, device_ids=[self.cfg.local_rank],
                output_device=self.cfg.local_rank,
                find_unused_parameters=False)
            
            self.D = DistributedDataParallel(
                self.D, device_ids=[self.cfg.local_rank],
                output_device=self.cfg.local_rank,
                find_unused_parameters=False)
            self.logger.info("Set DistributedDataParallel")


class Trainer(DDPTrainer):
    """
    Base Trainer class
    """

    def __init__(
        self,
        generator,
        discriminator,
        config,
        logger,
    ) -> None:
        self.G = generator
        self.D = discriminator
        self.cfg = config
        self.max_norm = 1
        self.device_type = 'cuda' if self.cfg.device.startswith('cuda') else 'cpu'
        self.optimizer_g = optim.Adam(self.G.parameters(), lr=self.cfg.lr_g, betas=(0.5, 0.999))
        self.optimizer_d = optim.Adam(self.D.parameters(), lr=self.cfg.lr_d, betas=(0.5, 0.999))
        self.loss_tracker = LossSummary()
        if self.cfg.ddp:
            self.device = torch.device(f"cuda:{self.cfg.local_rank}")
            logger.info(f"---------{self.cfg.local_rank} {self.device}")
        else:
            self.device = torch.device(self.cfg.device)
        self.loss_fn = AnimeGanLoss(self.cfg, self.device)
        self.logger = logger
        self._init_working_dir()
        self._init_distributed()
        self._init_amp(enabled=self.cfg.amp)

    def _init_working_dir(self):
        """Init working directory for saving checkpoint, ..."""
        os.makedirs(self.cfg.exp_dir, exist_ok=True)
        Gname = self.G.name
        Dname = self.D.name
        self.checkpoint_path_G_init = os.path.join(self.cfg.exp_dir, f"{Gname}_init.pt")
        self.checkpoint_path_G = os.path.join(self.cfg.exp_dir, f"{Gname}.pt")
        self.checkpoint_path_D = os.path.join(self.cfg.exp_dir, f"{Dname}.pt")
        self.save_image_dir = os.path.join(self.cfg.exp_dir, "generated_images")
        self.example_image_dir = os.path.join(self.cfg.exp_dir, "train_images")
        os.makedirs(self.save_image_dir, exist_ok=True)
        os.makedirs(self.example_image_dir, exist_ok=True)

    def init_weight_G(self, weight: str):
        """Init Generator weight"""
        return load_checkpoint(self.G, weight)

    def init_weight_D(self, weight: str):
        """Init Discriminator weight"""
        return load_checkpoint(self.D, weight)

    def pretrain_generator(self, train_loader, start_epoch):
        """
        Pretrain Generator to recontruct input image.
        """
        init_losses = []
        set_lr(self.optimizer_g, self.cfg.init_lr)
        for epoch in range(start_epoch, self.cfg.init_epochs):
            # Train with content loss only
            
            pbar = tqdm(train_loader)
            for data in pbar:
                img = data["image"].to(self.device)

                self.optimizer_g.zero_grad()

                with autocast(enabled=self.cfg.amp):
                    fake_img = self.G(img)
                    loss = self.loss_fn.content_loss_vgg(img, fake_img)

                self.scaler_g.scale(loss).backward()
                self.scaler_g.step(self.optimizer_g)
                self.scaler_g.update()

                if self.cfg.ddp:
                    torch.distributed.barrier()

                init_losses.append(loss.cpu().detach().numpy())
                avg_content_loss = sum(init_losses) / len(init_losses)
                pbar.set_description(f'[Init Training G] content loss: {avg_content_loss:2f}')

            save_checkpoint(self.G, self.checkpoint_path_G_init, self.optimizer_g, epoch)
            if self.cfg.local_rank == 0:
                self.generate_and_save(self.cfg.test_image_dir, subname='initg')
                self.logger.info(f"Epoch {epoch}/{self.cfg.init_epochs}")

        set_lr(self.optimizer_g, self.cfg.lr_g)

    def train_epoch(self, epoch, train_loader):
        pbar = tqdm(train_loader, total=len(train_loader))
        for data in pbar:
            img = data["image"].to(self.device)
            anime = data["anime"].to(self.device)
            anime_gray = data["anime_gray"].to(self.device)
            anime_smt_gray = data["smooth_gray"].to(self.device)

            # ---------------- TRAIN D ---------------- #
            self.optimizer_d.zero_grad()

            with autocast(enabled=self.cfg.amp):
                fake_img = self.G(img)
                # Add some Gaussian noise to images before feeding to D
                if self.cfg.d_noise:
                    fake_img += gaussian_noise()
                    anime += gaussian_noise()
                    anime_gray += gaussian_noise()
                    anime_smt_gray += gaussian_noise()
                fake_img_color_mapped = transfer_color_and_rescale(fake_img, anime)
                # Log
                # save_generated_images(fake_img, "debug")
                # save_generated_images(anime, "debug_anime")
                # save_generated_images(fake_img_color_mapped, "debug_color")
                # raise
                
                fake_d = self.D(fake_img_color_mapped)
                real_anime_d = self.D(anime)
                real_anime_gray_d = self.D(anime_gray)
                real_anime_smt_gray_d = self.D(anime_smt_gray)

                loss_d = self.loss_fn.compute_loss_D(
                    fake_d,
                    real_anime_d,
                    real_anime_gray_d,
                    real_anime_smt_gray_d
                )

            self.scaler_d.scale(loss_d).backward()
            self.scaler_d.unscale_(self.optimizer_d)
            torch.nn.utils.clip_grad_norm_(self.D.parameters(), max_norm=self.max_norm)
            self.scaler_d.step(self.optimizer_d)
            self.scaler_d.update()
            if self.cfg.ddp:
                torch.distributed.barrier()
            self.loss_tracker.update_loss_D(loss_d)

            # ---------------- TRAIN G ---------------- #
            self.optimizer_g.zero_grad()

            with autocast(enabled=self.cfg.amp):
                fake_img = self.G(img)
                fake_img_color_mapped = transfer_color_and_rescale(fake_img, anime)
                fake_d = self.D(fake_img_color_mapped)

                (
                    adv_loss, con_loss,
                    gra_loss, col_loss,
                    tv_loss
                ) = self.loss_fn.compute_loss_G(
                    fake_img,
                    img,
                    fake_d,
                    anime_gray,
                )
                loss_g = adv_loss + con_loss + gra_loss + col_loss + tv_loss
                if torch.isnan(adv_loss).any():
                    self.logger.info("----------------------------------------------")
                    self.logger.info(fake_d)
                    self.logger.info(adv_loss)
                    self.logger.info("----------------------------------------------")
                    raise ValueError("NAN loss!!")

            self.scaler_g.scale(loss_g).backward()
            self.scaler_d.unscale_(self.optimizer_g)
            grad = torch.nn.utils.clip_grad_norm_(self.G.parameters(), max_norm=self.max_norm)
            self.scaler_g.step(self.optimizer_g)
            self.scaler_g.update()
            if self.cfg.ddp:
                torch.distributed.barrier()

            self.loss_tracker.update_loss_G(adv_loss, gra_loss, col_loss, con_loss)
            pbar.set_description(f"{self.loss_tracker.get_loss_description()} - {grad:.3f}")

    def get_train_loader(self, dataset):
        if self.cfg.ddp:
            train_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
        else:
            train_sampler = None
        return DataLoader(
            dataset,
            batch_size=self.cfg.batch_size,
            num_workers=self.cfg.num_workers,
            pin_memory=True,
            shuffle=train_sampler is None,
            sampler=train_sampler,
            drop_last=True,
            # collate_fn=collate_fn,
        )

    def train(self, train_dataset: Dataset, start_epoch=0, start_epoch_g=0):
        """
        Train Generator and Discriminator.
        """
        self.logger.info(self.device)
        self.G.to(self.device)
        self.D.to(self.device)

        self.pretrain_generator(self.get_train_loader(train_dataset), start_epoch_g)

        if self.cfg.local_rank == 0:
            self.logger.info(f"Start training for {self.cfg.epochs} epochs")

        for i, data in enumerate(train_dataset):
            for k in data.keys():
                image = data[k]
                cv2.imwrite(
                    os.path.join(self.example_image_dir, f"data_{k}_{i}.jpg"),
                    revert_to_np_image(image)
                )
            if i == 2:
                break

        end = None
        num_iter = 0
        per_epoch_times = []
        for epoch in range(start_epoch, self.cfg.epochs):
            start = time.time()
            self.train_epoch(epoch, self.get_train_loader(train_dataset))

            if epoch % self.cfg.save_interval == 0 and self.cfg.local_rank == 0:
                save_checkpoint(self.G, self.checkpoint_path_G,self.optimizer_g, epoch)
                save_checkpoint(self.D, self.checkpoint_path_D, self.optimizer_d, epoch)
                self.generate_and_save(self.cfg.test_image_dir)
            num_iter += 1

            if self.cfg.local_rank == 0:
                end = time.time()
                if end is None:
                    eta = 9999
                else:
                    per_epoch_time = (end - start)
                    per_epoch_times.append(per_epoch_time)
                    eta = np.mean(per_epoch_times) * (self.cfg.epochs - epoch)
                    eta = convert_to_readable(eta)
                self.logger.info(f"epoch {epoch}/{self.cfg.epochs}, ETA: {eta}")

    def generate_and_save(
        self,
        image_dir,
        max_imgs=15,
        subname='gen'
    ):
        '''
        Generate and save images
        '''
        self.G.eval()

        max_iter = max_imgs
        fake_imgs = []

        image_files = glob(os.path.join(image_dir, "*"))

        for i, image_file in enumerate(image_files):
            image = read_image(image_file)
            image = resize_image(image)
            image = preprocess_images(image)
            image = image.to(self.device)
            with torch.no_grad():
                with autocast(enabled=self.cfg.amp):
                    fake_img = self.G(image)
                fake_img = fake_img.detach().cpu().numpy()
                # Channel first -> channel last
                fake_img  = fake_img.transpose(0, 2, 3, 1)
                fake_imgs.append(denormalize_input(fake_img, dtype=np.int16)[0])

            if i + 1 == max_iter:
                break

        # fake_imgs = np.concatenate(fake_imgs, axis=0)

        for i, img in enumerate(fake_imgs):
            save_path = os.path.join(self.save_image_dir, f'{subname}_{i}.jpg')
            if not cv2.imwrite(save_path, img[..., ::-1]):
                self.logger.info(f"Save generated image failed, {save_path}, {img.shape}")
