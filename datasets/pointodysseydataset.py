from numpy import random
import torch
import numpy as np
import os
import torchvision.transforms as transforms
import torch.nn.functional as F
from PIL import Image
import random
from torch._C import dtype, set_flush_denormal
import utils.basic
import utils.misc
import utils.improc
import glob
import cv2
from torchvision.transforms import ColorJitter, GaussianBlur
import albumentations as A
from functools import partial
import sys

# np.random.seed(125)
# torch.multiprocessing.set_sharing_strategy('file_system')


def augment_video(augmenter, **kwargs):
    assert isinstance(augmenter, A.ReplayCompose)
    keys = kwargs.keys()
    for i in range(len(next(iter(kwargs.values())))):
        data = augmenter(**{
            key: kwargs[key][i] if key not in ['bboxes', 'keypoints'] else [kwargs[key][i]] for key in keys
        })
        if i == 0:
            augmenter = partial(A.ReplayCompose.replay, data['replay'])
        for key in keys:
            if key == 'bboxes':
                kwargs[key][i] = np.array(data[key]).reshape(4)
            elif key == 'keypoints':
                kwargs[key][i] = np.array(data[key]).reshape(2)
            else:
                kwargs[key][i] = data[key]
                
class PointOdysseyDataset(torch.utils.data.Dataset):
    def __init__(self,
                 dataset_location='/orion/group/point_odyssey',
                 dset='train',
                 use_augs=False,
                 S=8,
                 N=32,
                 crop_size=(368, 496),
                 quick=False,
                 verbose=False,
                 load_3d=False,
    ):
        print('loading pointodyssey dataset...')

        self.S = S
        self.N = N
        self.load_3d = load_3d

        self.use_augs = use_augs
        self.dset = dset

        self.rgb_paths = []
        self.depth_paths = []
        self.traj_paths = []
        self.annotation_paths = []
        self.full_idxs = []

        self.subdirs = []
        self.sequences = []
        
        assert(dset in ['train', 'val', 'test'])
        self.subdirs.append(os.path.join(dataset_location, dset))

        for subdir in self.subdirs:
            for seq in glob.glob(os.path.join(subdir, "*")):
                seq_name = seq.split('/')[-1]
                self.sequences.append(seq)

        self.sequences = sorted(self.sequences)
        if verbose:
            print(self.sequences)
        print('found %d unique videos in %s (dset=%s)' % (len(self.sequences), dataset_location, dset))
        
        ## load trajectories
        print('loading trajectories...')

        if quick:
           self.sequences = self.sequences[:1] 
        
        for seq in self.sequences:
            
            rgb_path = os.path.join(seq, 'rgbs')

            annotations_path = os.path.join(seq, 'annotations.npz')
            if os.path.isfile(annotations_path):

                if verbose: 
                    print('seq', seq)
                    
                for stride in [1,2,3,4]:
                    for ii in range(0,len(os.listdir(rgb_path))-self.S*stride+1, 4):
                        full_idx = ii + np.arange(self.S)*stride
                        self.rgb_paths.append([os.path.join(seq, 'rgbs', 'rgb_%05d.jpg' % idx) for idx in full_idx])
                        # self.depth_paths.append([os.path.join(seq, 'depths', 'depth_%05d.png' % idx) for idx in full_idx])
                        self.annotation_paths.append(os.path.join(seq, 'annotations.npz'))
                        self.full_idxs.append(full_idx)
                        if verbose:
                            sys.stdout.write('.')
                            sys.stdout.flush()
                        else:
                            if verbose:
                                sys.stdout.write('v')
                                sys.stdout.flush()

        print('collected %d clips of length %d in %s (dset=%s)' % (
            len(self.rgb_paths), self.S, dataset_location, dset))

        # photometric augmentation
        self.photo_aug = ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.25 / 3.14)

        self.spatial_aug_prob = 0.7
        self.reverse_prob = 0.5
        
        self.perframe_color_aug_prob = 0.2

        # occlusion augmentation
        self.eraser_aug_prob = 0.2
        self.eraser_bounds = [20, 300]

        # spatial augmentations
        self.pad_bounds = [0, 64]
        self.crop_size = crop_size
        self.resize_lim = [0.25, 1.5] # sample resizes from here
        self.resize_delta = 0.1
        self.max_crop_offset = 20
        
        self.do_flip = True
        self.h_flip_prob = 0.5
        self.v_flip_prob = 0.5

        self.color_augmenter = A.ReplayCompose([
            A.GaussNoise(p=0.2),
            A.OneOf([
                A.MotionBlur(p=0.2),
                A.MedianBlur(blur_limit=3, p=0.1),
                A.Blur(blur_limit=3, p=0.1),
            ], p=0.2),
            A.OneOf([
                A.CLAHE(clip_limit=2),
                A.Sharpen(),
                A.Emboss(),
            ], p=0.2),
            A.RGBShift(p=0.5),
            A.RandomBrightnessContrast(p=0.5),
            A.RandomGamma(p=0.5),
            A.HueSaturationValue(p=0.3),
            A.ImageCompression(quality_lower=50, quality_upper=100, p=0.3),
        ], p=0.8)
        

    def getitem_helper(self, index):
        sample = None
        gotit = False

        rgb_paths = self.rgb_paths[index]
        # depth_paths = self.depth_paths[index]
        full_idx = self.full_idxs[index]
        annotations_path = self.annotation_paths[index]
        annotations = np.load(annotations_path, allow_pickle=True)
        # print(annotations.files)
        trajs = annotations['trajs_2d'][full_idx].astype(np.float32)
        visibs = annotations['visibilities'][full_idx].astype(np.float32)
        valids = (visibs<2).astype(np.float32)
        visibs = (visibs==1).astype(np.float32)

        trajs_world = annotations['trajs_3d'][full_idx].astype(np.float32)
        pix_T_cams = annotations['intrinsics'][full_idx].astype(np.float32)
        cams_T_world = annotations['extrinsics'][full_idx].astype(np.float32)
        
        trajs_cams = utils.geom.apply_4x4_py(cams_T_world, trajs_world) # S,N,3
        # these are the trajectories in the camera coordinate systems
        # we will use these to discard trajectories that travel behind the camera,
        # because projection gives unusable results on these
        zs = trajs_cams[:,:,2] # S,N
        min_z = np.min(zs, axis=0) # N
        z_ok = min_z > 1e-4
        trajs = trajs[:,z_ok]
        visibs = visibs[:,z_ok]
        valids = valids[:,z_ok]

        S,N,D = trajs.shape
        assert(D==2)
        assert(S==self.S)

        if N < self.N:
            print('returning before cropping: N=%d; need N=%d' % (N, self.N))
            return None, False

        valids_xy = np.ones_like(trajs)

        # get rid of infs and nans
        inf_idx = np.where(np.isinf(trajs))
        trajs[inf_idx] = 0
        valids_xy[inf_idx] = 0
        nan_idx = np.where(np.isnan(trajs))
        trajs[nan_idx] = 0
        valids_xy[nan_idx] = 0

        inv_idx = np.where(np.sum(valids_xy, axis=2)<2) # S,N
        visibs[inv_idx] = 0
        valids[inv_idx] = 0

        rgbs = []
        for rgb_path in rgb_paths:
            with Image.open(rgb_path) as im:
                rgbs.append(np.array(im)[:, :, :3])

        # if self.load_3d:
        #     depths = []
        #     for depth_path in depth_paths:
        #         depth16 = cv2.imread(depth_path, cv2.IMREAD_ANYDEPTH)
        #         depth = depth16.astype(np.float32) / 65535.0 * 1000.0
        #         depths.append(depth)
        # else:
        #     depths = None
                
        if self.use_augs:
            rgbs, trajs, visibs = self.add_photometric_augs(rgbs, trajs, visibs)

            if np.random.rand() < self.reverse_prob:
                rgbs = np.stack(rgbs, 0)
                # depths = np.stack(depths, 0)
                rgbs = np.flip(rgbs, axis=0)
                trajs = np.flip(trajs, axis=0)
                visibs = np.flip(visibs, axis=0)
                rgbs = [rgb for rgb in rgbs]

                # if self.load_3d:
                #     depths = np.stack(depths, 0)
                #     depths = np.flip(depths, axis=0)
                #     depths = [depth for depth in depths]

        if self.use_augs and (np.random.rand() < self.spatial_aug_prob):
            rgbs, trajs = self.add_spatial_augs(rgbs, trajs, visibs)
        else:
            if np.random.rand() < 0.5: # crop
                rgbs, trajs = self.just_crop(rgbs, trajs)
                # if index % 2 == 0:
            else:
                # else:
                rgbs, trajs = self.just_resize(rgbs, trajs)
            # rgbs, trajs, depths = self.just_resize(rgbs, trajs, depths)

        H,W,C = rgbs[0].shape
        assert(C==3)
        
        # update visibility annotations
        for si in range(S):
            # avoid 1px edge
            oob_inds = np.logical_or(
                np.logical_or(trajs[si,:,0] < 1, trajs[si,:,0] > W-2),
                np.logical_or(trajs[si,:,1] < 1, trajs[si,:,1] > H-2))
            visibs[si,oob_inds] = 0

            # when a point moves far oob, don't supervise with it
            very_oob_inds = np.logical_or(
                np.logical_or(trajs[si,:,0] < -32, trajs[si,:,0] > W+32),
                np.logical_or(trajs[si,:,1] < -32, trajs[si,:,1] > H+32))
            valids[si,very_oob_inds] = 0

        # ensure that the point is good at frame0
        vis_and_val = valids * visibs
        vis0 = vis_and_val[0] > 0
        trajs = trajs[:,vis0]
        visibs = visibs[:,vis0]
        valids = valids[:,vis0]

        # ensure that the point is good at frame1
        vis_and_val = valids * visibs
        vis1 = vis_and_val[1] > 0
        trajs = trajs[:,vis1]
        visibs = visibs[:,vis1]
        valids = valids[:,vis1]

        # ensure that the point is good in at least sqrt(S) frames
        vis_and_val = valids * visibs
        vis_ok = np.sum(vis_and_val, axis=0) >= max(np.sqrt(S),2)
        trajs = trajs[:,vis_ok]
        visibs = visibs[:,vis_ok]
        valids = valids[:,vis_ok]

        # ensure that the per-frame motion isn't too crazy
        mot = np.max(np.linalg.norm(trajs[1:] - trajs[:-1], axis=-1), axis=0) # N
        # print('min', np.min(mot), 'max', np.max(mot))
        mot_ok = mot < 128
        if np.sum(~mot_ok):
            print('sum(~mot_ok)', np.sum(~mot_ok))
        trajs = trajs[:,mot_ok]
        visibs = visibs[:,mot_ok]
        valids = valids[:,mot_ok]
        
        N = trajs.shape[1]
        
        if N < self.N//2:
            print('N=%d' % (N))
            return None, False
        
        if N < self.N:
            print('N=%d; ideally we want N=%d, but we will pad' % (N, self.N))

        if N > self.N*4:
            # fps based on position and motion
            xym = np.concatenate([np.mean(trajs, axis=0), np.mean(trajs[1:] - trajs[:-1], axis=0)], axis=-1)
            inds = utils.misc.farthest_point_sample_py(xym, self.N*4)
            trajs = trajs[:,inds]
            visibs = visibs[:,inds]
            valids = valids[:,inds]

        # we won't supervise with the extremes, but let's clamp anyway just to be safe
        trajs = np.minimum(np.maximum(trajs, np.array([-32,-32])), np.array([W+31, H+31])) # S,N,2
        
        N = trajs.shape[1]
        N_ = min(N, self.N)
        inds = np.random.choice(N, N_, replace=False)

        # prep for batching, by fixing N
        trajs_full = np.zeros((self.S, self.N, 2)).astype(np.float32)
        visibs_full = np.zeros((self.S, self.N)).astype(np.float32)
        valids_full = np.zeros((self.S, self.N)).astype(np.float32)

        trajs_full[:,:N_] = trajs[:,inds]
        visibs_full[:,:N_] = visibs[:,inds]
        valids_full[:,:N_] = valids[:,inds]
        

        rgbs = torch.from_numpy(np.stack(rgbs, 0)).permute(0,3,1,2)  # S, C, H, W
        trajs = torch.from_numpy(trajs_full)  # S, N, 2
        visibs = torch.from_numpy(visibs_full)  # S, N
        valids = torch.from_numpy(valids_full)  # S, N

        sample = {
            'rgbs': rgbs,
            'trajs': trajs,
            'visibs': visibs,
            'valids': valids,
        }
        
        
        # if self.load_3d:
        #     depths = torch.from_numpy(np.stack(depths, 0)).unsqueeze(1)  # S,1,H,W
        #     sample['depths'] = depths
        return sample, True

    
    def __getitem__(self, index):
        gotit = False
        sample, gotit = self.getitem_helper(index)
        if not gotit:
            print('warning: sampling failed')
            # return a fake sample, so we can still collate
            sample = {
                'rgbs': torch.zeros((self.S, 3, self.crop_size[0], self.crop_size[1])),
                'trajs': torch.zeros((self.S, self.N, 2)),
                'visibs': torch.zeros((self.S, self.N)),
                'valids': torch.zeros((self.S, self.N)),
            }
        return sample, gotit
    

    def add_photometric_augs(self, rgbs, trajs, visibs):
        T, N, _ = trajs.shape

        # print('trajs', trajs.shape)
        # print('len(rgbs)', len(rgbs))

        S = len(rgbs)
        H, W = rgbs[0].shape[:2]
        assert (S == T)

        # ############ eraser transform (per image after the first) ############
        # rgbs = [rgb.astype(np.float32) for rgb in rgbs]
        # for i in range(1, S):
        #     if np.random.rand() < self.eraser_aug_prob:
        #         mean_color = np.mean(rgbs[i].reshape(-1, 3), axis=0)
        #         for _ in range(np.random.randint(1, 3)):  # number of times to occlude
        #             xc = np.random.randint(0, W)
        #             yc = np.random.randint(0, H)
        #             dx = np.random.randint(self.eraser_bounds[0], self.eraser_bounds[1])
        #             dy = np.random.randint(self.eraser_bounds[0], self.eraser_bounds[1])
        #             x0 = np.clip(xc - dx / 2, 0, W - 1).round().astype(np.int32)
        #             x1 = np.clip(xc + dx / 2, 0, W - 1).round().astype(np.int32)
        #             y0 = np.clip(yc - dy / 2, 0, W - 1).round().astype(np.int32)
        #             y1 = np.clip(yc + dy / 2, 0, W - 1).round().astype(np.int32)
        #             # print(x0, x1, y0, y1)
        #             rgbs[i][y0:y1, x0:x1, :] = mean_color

        #             occ_inds = np.logical_and(np.logical_and(trajs[i, :, 0] >= x0, trajs[i, :, 0] < x1),
        #                                       np.logical_and(trajs[i, :, 1] >= y0, trajs[i, :, 1] < y1))
        #             visibs[i, occ_inds] = 0
        # rgbs = [rgb.astype(np.uint8) for rgb in rgbs]

        ############ photometric augmentation ############
        rgbs = np.stack(rgbs, 0)
        augment_video(self.color_augmenter, image=rgbs)
        rgbs = [rgb for rgb in rgbs]
        
        # if np.random.rand() < self.perframe_color_aug_prob:
        #     rgbs = [np.array(self.photo_aug(Image.fromarray(rgb)), dtype=np.uint8) for rgb in rgbs]

        return rgbs, trajs, visibs
    

    def add_spatial_augs(self, rgbs, trajs, visibs):
        T, N, _ = trajs.shape
        
        S = len(rgbs)
        H, W = rgbs[0].shape[:2]
        assert(S==T)

        rgbs = [rgb.astype(np.float32) for rgb in rgbs]
        
        # padding
        pad_x0 = np.random.randint(self.pad_bounds[0], self.pad_bounds[1])
        pad_x1 = np.random.randint(self.pad_bounds[0], self.pad_bounds[1])
        pad_y0 = np.random.randint(self.pad_bounds[0], self.pad_bounds[1])
        pad_y1 = np.random.randint(self.pad_bounds[0], self.pad_bounds[1])

        rgbs = [np.pad(rgb, ((pad_y0, pad_y1), (pad_x0, pad_x1), (0, 0))) for rgb in rgbs]
        trajs[:,:,0] += pad_x0
        trajs[:,:,1] += pad_y0
        H, W = rgbs[0].shape[:2]

        # scaling + stretching
        scale = np.random.uniform(self.resize_lim[0], self.resize_lim[1])
        scale_x = scale
        scale_y = scale
        H_new = H
        W_new = W

        scale_delta_x = 0.0
        scale_delta_y = 0.0

        rgbs_scaled = []
        trajs_scaled = []
        
        scales_x = []
        scales_y = []
        for si in range(S):
            if si==1:
                scale_delta_x = np.random.uniform(-self.resize_delta, self.resize_delta)*0.1
                scale_delta_y = np.random.uniform(-self.resize_delta, self.resize_delta)*0.1
            elif si > 1:
                scale_delta_x = scale_delta_x*0.9 + np.random.uniform(-self.resize_delta, self.resize_delta)*0.1
                scale_delta_y = scale_delta_y*0.9 + np.random.uniform(-self.resize_delta, self.resize_delta)*0.1
            scale_x = scale_x + scale_delta_x
            scale_y = scale_y + scale_delta_y

            # bring h/w closer
            scale_xy = (scale_x + scale_y)*0.5
            scale_x = scale_x*0.5 + scale_xy*0.5
            scale_y = scale_y*0.5 + scale_xy*0.5
            
            # don't get too crazy
            scale_x = np.clip(scale_x, 0.2, 2.0)
            scale_y = np.clip(scale_y, 0.2, 2.0)
            
            H_new = int(H * scale_y)
            W_new = int(W * scale_x)

            # make it at least slightly bigger than the crop area,
            # so that the random cropping can add diversity
            H_new = np.clip(H_new, self.crop_size[0]+16, None)
            W_new = np.clip(W_new, self.crop_size[1]+16, None)
            # recompute scale in case we clipped
            scale_x = W_new/float(W)
            scale_y = H_new/float(H)

            rgbs_scaled.append(cv2.resize(rgbs[si], (W_new, H_new), interpolation=cv2.INTER_LINEAR))
            trajs[si,:,0] *= scale_x
            trajs[si,:,1] *= scale_y
        rgbs = rgbs_scaled
        
        ok_inds = visibs[0,:] > 0
        vis_trajs = trajs[:,ok_inds] # S,?,2
            
        if vis_trajs.shape[1] > 0:
            mid_x = np.mean(vis_trajs[0,:,0])
            mid_y = np.mean(vis_trajs[0,:,1])
        else:
            mid_y = self.crop_size[0]
            mid_x = self.crop_size[1]
            
        x0 = int(mid_x - self.crop_size[1]//2)
        y0 = int(mid_y - self.crop_size[0]//2)
        
        offset_x = 0
        offset_y = 0
        
        for si in range(S):
            # on each frame, shift a bit more 
            if si==1:
                offset_x = np.random.randint(-self.max_crop_offset, self.max_crop_offset)
                offset_y = np.random.randint(-self.max_crop_offset, self.max_crop_offset)
            elif si > 1:
                offset_x = int(offset_x*0.8 + np.random.randint(-self.max_crop_offset, self.max_crop_offset+1)*0.2)
                offset_y = int(offset_y*0.8 + np.random.randint(-self.max_crop_offset, self.max_crop_offset+1)*0.2)
            x0 = x0 + offset_x
            y0 = y0 + offset_y

            H_new, W_new = rgbs[si].shape[:2]
            if H_new==self.crop_size[0]:
                y0 = 0
            else:
                y0 = min(max(0, y0), H_new - self.crop_size[0] - 1)
                
            if W_new==self.crop_size[1]:
                x0 = 0
            else:
                x0 = min(max(0, x0), W_new - self.crop_size[1] - 1)
            rgbs[si] = rgbs[si][y0:y0+self.crop_size[0], x0:x0+self.crop_size[1]]
            trajs[si,:,0] -= x0
            trajs[si,:,1] -= y0

            
        H_new = self.crop_size[0]
        W_new = self.crop_size[1]

        # flip
        h_flipped = False
        v_flipped = False
        if self.do_flip:
            # h flip
            if np.random.rand() < self.h_flip_prob:
                # print('h flip')
                h_flipped = True
                rgbs = [rgb[:,::-1] for rgb in rgbs]
            # v flip
            if np.random.rand() < self.v_flip_prob:
                # print('v flip')
                v_flipped = True
                rgbs = [rgb[::-1] for rgb in rgbs]
        if h_flipped:
            trajs[:,:,0] = W_new - trajs[:,:,0]
        if v_flipped:
            trajs[:,:,1] = H_new - trajs[:,:,1]
            
        return rgbs, trajs

    def just_crop(self, rgbs, trajs):
        T, N, _ = trajs.shape
        
        S = len(rgbs)
        H, W = rgbs[0].shape[:2]
        assert(S==T)

        H_new, W_new = self.crop_size[0], self.crop_size[1]

        if np.random.rand() < 0.5:
            # simple random crop
            y0 = np.random.randint(0, H-H_new)
            x0 = np.random.randint(0, W-W_new)
            rgbs = [rgb[y0:y0+H_new, x0:x0+W_new] for rgb in rgbs]
            trajs[:,:,0] -= x0
            trajs[:,:,1] -= y0
        else:
            sx_ = W_new / W
            sy_ = H_new / H
            rgbs = [cv2.resize(rgb, (W_new, H_new), interpolation=cv2.INTER_LINEAR) for rgb in rgbs]
            sc_py = np.array([sx_, sy_]).reshape([1,1,2])
            trajs = trajs * sc_py
            
        return rgbs, trajs

    def just_resize(self, rgbs, trajs):#, depths=None):
        T, N, _ = trajs.shape
        
        S = len(rgbs)
        H, W = rgbs[0].shape[:2]
        assert(S==T)

        H_new, W_new = self.crop_size[0], self.crop_size[1]

        sx_ = W_new / W
        sy_ = H_new / H
        rgbs = [cv2.resize(rgb, (W_new, H_new), interpolation=cv2.INTER_LINEAR) for rgb in rgbs]
        sc_py = np.array([sx_, sy_]).reshape([1,1,2])
        trajs = trajs * sc_py

        # if depths is not None:
        #     depths = [cv2.resize(depth, (W_new, H_new), interpolation=cv2.INTER_NEAREST) for depth in depths]
        
        return rgbs, trajs#, depths
    

    def __len__(self):
        return len(self.rgb_paths)