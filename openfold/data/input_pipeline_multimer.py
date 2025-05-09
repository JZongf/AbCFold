# Copyright 2021 AlQuraishi Laboratory
# Copyright 2021 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import random
import torch

from openfold.data import (
    data_transforms,
    data_transforms_multimer,
    sample_msa
)


def nonensembled_transform_fns(common_cfg, mode_cfg):
    """Input pipeline data transformers that are not ensembled."""
    transforms = [
        data_transforms.cast_to_64bit_ints,
        data_transforms_multimer.make_msa_profile,
        data_transforms_multimer.create_target_feat,
        data_transforms.make_atom14_masks,
    ]

    if mode_cfg.supervised:
        transforms.extend(
            [
                data_transforms.make_atom14_positions,
                data_transforms.atom37_to_frames,
                data_transforms.atom37_to_torsion_angles(""),
                data_transforms.make_pseudo_beta(""),
                data_transforms.get_backbone_frames,
                data_transforms.get_chi_angles,
            ]
        )

    return transforms


def ensembled_transform_fns(
    common_cfg, 
    mode_cfg, 
    ensemble_seed, 
    iter_idx, 
    max_iters, 
    pair_msa_num=None, 
    chain1_msa_num=None, 
    chain2_msa_num=None, 
    region_index=None, 
    cluster=False, 
    msa_cluster_idx=None, 
    msas_num=None
):
    """Input pipeline data transformers that can be ensembled and averaged."""
    transforms = []

    pad_msa_clusters = mode_cfg.max_msa_clusters
    max_msa_clusters = pad_msa_clusters
    max_extra_msa = mode_cfg.max_extra_msa

    msa_seed = None
    if(not common_cfg.resample_msa_in_recycling):
        msa_seed = ensemble_seed
    
    if cluster:
        transforms.append(
            sample_msa.sample_msa_cluster(
                max_msa_clusters, 
                max_extra_msa,
                iter_idx=iter_idx,
                region_index=region_index,
                region_mask=False,
                msa_cluster_idx=msa_cluster_idx,
                fix_cluster_size=common_cfg.fix_cluster_size,
            )
        )
        
    else:
        if pair_msa_num is not None:
            transforms.append(
                sample_msa.sample_msa2_multimer(
                    max_msa_clusters, 
                    max_extra_msa,
                    iter_idx=iter_idx,
                    max_iter=max_iters,
                    region_index=region_index,
                    region_mask=False,
                    msas_num=msas_num
                )
            )
        else:
            transforms.append(
                sample_msa.sample_msa2(
                    max_msa_clusters, 
                    max_extra_msa,
                    iter_idx=iter_idx,
                    max_iter=max_iters,
                    region_index=region_index,
                    region_mask=False,
                )
            )

    if "masked_msa" in common_cfg:
        # Masked MSA should come *before* MSA clustering so that
        # the clustering and full MSA profile do not leak information about
        # the masked locations and secret corrupted locations.
        transforms.append(
            data_transforms_multimer.make_masked_msa(
                common_cfg.masked_msa, 
                mode_cfg.masked_msa_replace_fraction,
                seed=(msa_seed + 1) if msa_seed else None,
            )
        )

    transforms.append(data_transforms_multimer.nearest_neighbor_clusters())
    transforms.append(data_transforms_multimer.create_msa_feat)

    crop_feats = dict(common_cfg.feat)

    if mode_cfg.fixed_size:
        transforms.append(data_transforms.select_feat(list(crop_feats)))

        if mode_cfg.crop:
            transforms.append(
                data_transforms_multimer.random_crop_to_size(
                    crop_size=mode_cfg.crop_size,
                    max_templates=mode_cfg.max_templates,
                    shape_schema=crop_feats,
                    spatial_crop_prob=mode_cfg.spatial_crop_prob,
                    interface_threshold=mode_cfg.interface_threshold,
                    subsample_templates=mode_cfg.subsample_templates,
                    seed=ensemble_seed + 1,
                )
            )
        transforms.append(
            data_transforms.make_fixed_size(
                shape_schema=crop_feats,
                msa_cluster_size=pad_msa_clusters,
                extra_msa_size=mode_cfg.max_extra_msa,
                num_res=mode_cfg.crop_size,
                num_templates=mode_cfg.max_templates,
                region_index=region_index,
            )
        )
    else:
        transforms.append(
            data_transforms.crop_templates(mode_cfg.max_templates)
        )

    return transforms


def process_tensors_from_config(
    tensors, 
    common_cfg, 
    mode_cfg, 
    pair_msa_num=None, 
    chain1_msa_num=None, 
    chain2_msa_num=None, 
    region_index=None, 
    cluster=False, 
    msa_cluster_idx=None, 
    msas_num=None
):
    """Based on the config, apply filters and transformations to the data."""

    ensemble_seed = random.randint(0, torch.iinfo(torch.int32).max)

    def wrap_ensemble_fn(data, i):
        """Function to be mapped over the ensemble dimension."""
        d = data.copy()
        fns = ensembled_transform_fns(
            common_cfg, 
            mode_cfg, 
            ensemble_seed,
            iter_idx=i.data.item(),
            max_iters=int(common_cfg.max_recycling_iters),
            pair_msa_num=pair_msa_num,
            chain1_msa_num=chain1_msa_num,
            chain2_msa_num=chain2_msa_num,
            region_index=region_index,
            msa_cluster_idx=msa_cluster_idx,
            cluster=cluster,
            msas_num=msas_num,
        )
        fn = compose(fns)
        d["ensemble_index"] = i
        return fn(d)

    no_templates = True
    if("template_aatype" in tensors):
        no_templates = tensors["template_aatype"].shape[0] == 0

    nonensembled = nonensembled_transform_fns(
        common_cfg,
        mode_cfg,
    )

    tensors = compose(nonensembled)(tensors)

    if("no_recycling_iters" in tensors):
        num_recycling = int(tensors["no_recycling_iters"])
    else:
        num_recycling = common_cfg.max_recycling_iters

    tensors = map_fn(
        lambda x: wrap_ensemble_fn(tensors, x), torch.arange(num_recycling)
    )

    return tensors


@data_transforms.curry1
def compose(x, fs):
    for f in fs:
        x = f(x)
    return x


def map_fn(fun, x):
    ensembles = [fun(elem) for elem in x]
    features = ensembles[0].keys()
    ensembled_dict = {}
    for feat in features:
        ensembled_dict[feat] = torch.stack(
            [dict_i[feat] for dict_i in ensembles], dim=-1
        )
    return ensembled_dict
