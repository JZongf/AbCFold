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

"""Functions for getting templates and calculating template features."""
import abc
import dataclasses
import datetime
import functools
import glob
import json
import logging
import os
import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import numpy as np

from openfold.data import parsers, mmcif_parsing
from openfold.data.errors import Error
from openfold.data.tools import kalign
from openfold.data.tools.utils import to_date
from openfold.np import residue_constants


class NoChainsError(Error):
    """An error indicating that template mmCIF didn't have any chains."""


class SequenceNotInTemplateError(Error):
    """An error indicating that template mmCIF didn't contain the sequence."""


class NoAtomDataInTemplateError(Error):
    """An error indicating that template mmCIF didn't contain atom positions."""


class TemplateAtomMaskAllZerosError(Error):
    """An error indicating that template mmCIF had all atom positions masked."""


class QueryToTemplateAlignError(Error):
    """An error indicating that the query can't be aligned to the template."""


class CaDistanceError(Error):
    """An error indicating that a CA atom distance exceeds a threshold."""


# Prefilter exceptions.
class PrefilterError(Exception):
    """A base class for template prefilter exceptions."""


class DateError(PrefilterError):
    """An error indicating that the hit date was after the max allowed date."""


class AlignRatioError(PrefilterError):
    """An error indicating that the hit align ratio to the query was too small."""


class DuplicateError(PrefilterError):
    """An error indicating that the hit was an exact subsequence of the query."""


class LengthError(PrefilterError):
    """An error indicating that the hit was too short."""


TEMPLATE_FEATURES = {
    "template_aatype": np.int64,
    "template_all_atom_mask": np.float32,
    "template_all_atom_positions": np.float32,
    "template_domain_names": np.object,
    "template_sequence": np.object,
    "template_sum_probs": np.float32,
}


def empty_template_feats(n_res):
    return {
        "template_aatype": np.zeros(
            (0, n_res, len(residue_constants.restypes_with_x_and_gap)),
            np.float32
        ),
        "template_all_atom_mask": np.zeros(
            (0, n_res, residue_constants.atom_type_num), np.float32
        ),
        "template_all_atom_positions": np.zeros(
            (0, n_res, residue_constants.atom_type_num, 3), np.float32
        ),
        "template_domain_names": np.array([''.encode()], dtype=np.object),
        "template_sequence": np.array([''.encode()], dtype=np.object),
        "template_sum_probs": np.zeros((0, 1), dtype=np.float32),
    }


def _get_pdb_id_and_chain(hit: parsers.TemplateHit) -> Tuple[str, str]:
    """Returns PDB id and chain id for an HHSearch Hit."""
    # PDB ID: 4 letters. Chain ID: 1+ alphanumeric letters or "." if unknown.
    id_match = re.match(r"[a-zA-Z\d]{4}_[a-zA-Z0-9.]+", hit.name)
    if not id_match:
        raise ValueError(f"hit.name did not start with PDBID_chain: {hit.name}")
    pdb_id, chain_id = id_match.group(0).split("_")
    return pdb_id.lower(), chain_id


def _is_after_cutoff(
    pdb_id: str,
    release_dates: Mapping[str, datetime.datetime],
    release_date_cutoff: Optional[datetime.datetime],
) -> bool:
    """Checks if the template date is after the release date cutoff.

    Args:
        pdb_id: 4 letter pdb code.
        release_dates: Dictionary mapping PDB ids to their structure release dates.
        release_date_cutoff: Max release date that is valid for this query.

    Returns:
        True if the template release date is after the cutoff, False otherwise.
    """
    pdb_id_upper = pdb_id.upper()
    if release_date_cutoff is None:
        raise ValueError("The release_date_cutoff must not be None.")
    if pdb_id_upper in release_dates:
        return release_dates[pdb_id_upper] > release_date_cutoff
    else:
        # Since this is just a quick prefilter to reduce the number of mmCIF files
        # we need to parse, we don't have to worry about returning True here.
        logging.info(
            "Template structure not in release dates dict: %s", pdb_id
        )
        return False


def _replace_obsolete_references(obsolete_mapping) -> Mapping[str, str]:
    """Generates a new obsolete by tracing all cross-references and store the latest leaf to all referencing nodes"""
    obsolete_new = {}
    obsolete_keys = obsolete_mapping.keys()

    def _new_target(k):
        v = obsolete_mapping[k]
        if v in obsolete_keys:
            return _new_target(v)
        return v
    
    for k in obsolete_keys:
        obsolete_new[k] = _new_target(k)

    return obsolete_new

def _parse_obsolete(obsolete_file_path: str) -> Mapping[str, str]:
    """Parses the data file from PDB that lists which PDB ids are obsolete."""
    with open(obsolete_file_path) as f:
        result = {}
        for line in f:
            line = line.strip()
            # We skip obsolete entries that don't contain a mapping to a new entry.
            if line.startswith("OBSLTE") and len(line) > 30:
                # Format:    Date      From     To
                # 'OBSLTE    31-JUL-94 116L     216L'
                from_id = line[20:24].lower()
                to_id = line[29:33].lower()
                result[from_id] = to_id
        return _replace_obsolete_references(result)


def generate_release_dates_cache(mmcif_dir: str, out_path: str):
    dates = {}
    for f in os.listdir(mmcif_dir):
        if f.endswith(".cif"):
            path = os.path.join(mmcif_dir, f)
            with open(path, "r") as fp:
                mmcif_string = fp.read()

            file_id = os.path.splitext(f)[0]
            mmcif = mmcif_parsing.parse(
                file_id=file_id, mmcif_string=mmcif_string
            )
            if mmcif.mmcif_object is None:
                logging.info(f"Failed to parse {f}. Skipping...")
                continue

            mmcif = mmcif.mmcif_object
            release_date = mmcif.header["release_date"]

            dates[file_id] = release_date

    with open(out_path, "r") as fp:
        fp.write(json.dumps(dates))


def _parse_release_dates(path: str) -> Mapping[str, datetime.datetime]:
    """Parses release dates file, returns a mapping from PDBs to release dates."""
    with open(path, "r") as fp:
        data = json.load(fp)

    return {
        pdb.upper(): to_date(v)
        for pdb, d in data.items()
        for k, v in d.items()
        if k == "release_date"
    }


def _assess_hhsearch_hit(
    hit: parsers.TemplateHit,
    hit_pdb_code: str,
    query_sequence: str,
    release_dates: Mapping[str, datetime.datetime],
    release_date_cutoff: datetime.datetime,
    max_subsequence_ratio: float = 0.95,
    min_align_ratio: float = 0.1,
) -> bool:
    """Determines if template is valid (without parsing the template mmcif file).

    Args:
        hit: HhrHit for the template.
        hit_pdb_code: The 4 letter pdb code of the template hit. This might be
            different from the value in the actual hit since the original pdb might
            have become obsolete.
        query_sequence: Amino acid sequence of the query.
        release_dates: Dictionary mapping pdb codes to their structure release
            dates.
        release_date_cutoff: Max release date that is valid for this query.
        max_subsequence_ratio: Exclude any exact matches with this much overlap.
        min_align_ratio: Minimum overlap between the template and query.

    Returns:
        True if the hit passed the prefilter. Raises an exception otherwise.

    Raises:
        DateError: If the hit date was after the max allowed date.
        AlignRatioError: If the hit align ratio to the query was too small.
        DuplicateError: If the hit was an exact subsequence of the query.
        LengthError: If the hit was too short.
    """
    aligned_cols = hit.aligned_cols
    align_ratio = aligned_cols / len(query_sequence)

    template_sequence = hit.hit_sequence.replace("-", "")
    length_ratio = float(len(template_sequence)) / len(query_sequence)

    if _is_after_cutoff(hit_pdb_code, release_dates, release_date_cutoff):
        date = release_dates[hit_pdb_code.upper()]
        raise DateError(
            f"Date ({date}) > max template date "
            f"({release_date_cutoff})."
        )

    if align_ratio <= min_align_ratio:
        raise AlignRatioError(
            "Proportion of residues aligned to query too small. "
            f"Align ratio: {align_ratio}."
        )

    # Check whether the template is a large subsequence or duplicate of original
    # query. This can happen due to duplicate entries in the PDB database.
    duplicate = (
        template_sequence in query_sequence
        and length_ratio > max_subsequence_ratio
    )

    if duplicate:
        raise DuplicateError(
            "Template is an exact subsequence of query with large "
            f"coverage. Length ratio: {length_ratio}."
        )

    if len(template_sequence) < 10:
        raise LengthError(
            f"Template too short. Length: {len(template_sequence)}."
        )

    return True


def _find_template_in_pdb(
    template_chain_id: str,
    template_sequence: str,
    mmcif_object: mmcif_parsing.MmcifObject,
) -> Tuple[str, str, int]:
    """Tries to find the template chain in the given pdb file.

    This method tries the three following things in order:
        1. Tries if there is an exact match in both the chain ID and the sequence.
             If yes, the chain sequence is returned. Otherwise:
        2. Tries if there is an exact match only in the sequence.
             If yes, the chain sequence is returned. Otherwise:
        3. Tries if there is a fuzzy match (X = wildcard) in the sequence.
             If yes, the chain sequence is returned.
    If none of these succeed, a SequenceNotInTemplateError is thrown.

    Args:
        template_chain_id: The template chain ID.
        template_sequence: The template chain sequence.
        mmcif_object: The PDB object to search for the template in.

    Returns:
        A tuple with:
        * The chain sequence that was found to match the template in the PDB object.
        * The ID of the chain that is being returned.
        * The offset where the template sequence starts in the chain sequence.

    Raises:
        SequenceNotInTemplateError: If no match is found after the steps described
            above.
    """
    # Try if there is an exact match in both the chain ID and the (sub)sequence.
    pdb_id = mmcif_object.file_id
    chain_sequence = mmcif_object.chain_to_seqres.get(template_chain_id)
    if chain_sequence and (template_sequence in chain_sequence):
        logging.info(
            "Found an exact template match %s_%s.", pdb_id, template_chain_id
        )
        mapping_offset = chain_sequence.find(template_sequence)
        return chain_sequence, template_chain_id, mapping_offset

    # Try if there is an exact match in the (sub)sequence only.
    for chain_id, chain_sequence in mmcif_object.chain_to_seqres.items():
        if chain_sequence and (template_sequence in chain_sequence):
            logging.info("Found a sequence-only match %s_%s.", pdb_id, chain_id)
            mapping_offset = chain_sequence.find(template_sequence)
            return chain_sequence, chain_id, mapping_offset

    # Return a chain sequence that fuzzy matches (X = wildcard) the template.
    # Make parentheses unnamed groups (?:_) to avoid the 100 named groups limit.
    regex = ["." if aa == "X" else "(?:%s|X)" % aa for aa in template_sequence]
    regex = re.compile("".join(regex))
    for chain_id, chain_sequence in mmcif_object.chain_to_seqres.items():
        match = re.search(regex, chain_sequence)
        if match:
            logging.info(
                "Found a fuzzy sequence-only match %s_%s.", pdb_id, chain_id
            )
            mapping_offset = match.start()
            return chain_sequence, chain_id, mapping_offset

    # No hits, raise an error.
    raise SequenceNotInTemplateError(
        "Could not find the template sequence in %s_%s. Template sequence: %s, "
        "chain_to_seqres: %s"
        % (
            pdb_id,
            template_chain_id,
            template_sequence,
            mmcif_object.chain_to_seqres,
        )
    )


def _realign_pdb_template_to_query(
    old_template_sequence: str,
    template_chain_id: str,
    mmcif_object: mmcif_parsing.MmcifObject,
    old_mapping: Mapping[int, int],
    kalign_binary_path: str,
) -> Tuple[str, Mapping[int, int]]:
    """Aligns template from the mmcif_object to the query.

    In case PDB70 contains a different version of the template sequence, we need
    to perform a realignment to the actual sequence that is in the mmCIF file.
    This method performs such realignment, but returns the new sequence and
    mapping only if the sequence in the mmCIF file is 90% identical to the old
    sequence.

    Note that the old_template_sequence comes from the hit, and contains only that
    part of the chain that matches with the query while the new_template_sequence
    is the full chain.

    Args:
        old_template_sequence: The template sequence that was returned by the PDB
            template search (typically done using HHSearch).
        template_chain_id: The template chain id was returned by the PDB template
            search (typically done using HHSearch). This is used to find the right
            chain in the mmcif_object chain_to_seqres mapping.
        mmcif_object: A mmcif_object which holds the actual template data.
        old_mapping: A mapping from the query sequence to the template sequence.
            This mapping will be used to compute the new mapping from the query
            sequence to the actual mmcif_object template sequence by aligning the
            old_template_sequence and the actual template sequence.
        kalign_binary_path: The path to a kalign executable.

    Returns:
        A tuple (new_template_sequence, new_query_to_template_mapping) where:
        * new_template_sequence is the actual template sequence that was found in
            the mmcif_object.
        * new_query_to_template_mapping is the new mapping from the query to the
            actual template found in the mmcif_object.

    Raises:
        QueryToTemplateAlignError:
        * If there was an error thrown by the alignment tool.
        * Or if the actual template sequence differs by more than 10% from the
            old_template_sequence.
    """
    aligner = kalign.Kalign(binary_path=kalign_binary_path)
    new_template_sequence = mmcif_object.chain_to_seqres.get(
        template_chain_id, ""
    )

    # Sometimes the template chain id is unknown. But if there is only a single
    # sequence within the mmcif_object, it is safe to assume it is that one.
    if not new_template_sequence:
        if len(mmcif_object.chain_to_seqres) == 1:
            logging.info(
                "Could not find %s in %s, but there is only 1 sequence, so "
                "using that one.",
                template_chain_id,
                mmcif_object.file_id,
            )
            new_template_sequence = list(mmcif_object.chain_to_seqres.values())[
                0
            ]
        else:
            raise QueryToTemplateAlignError(
                f"Could not find chain {template_chain_id} in {mmcif_object.file_id}. "
                "If there are no mmCIF parsing errors, it is possible it was not a "
                "protein chain."
            )

    try:
        parsed_a3m = parsers.parse_a3m(
            aligner.align([old_template_sequence, new_template_sequence])
        )
        old_aligned_template, new_aligned_template = parsed_a3m.sequences
    except Exception as e:
        raise QueryToTemplateAlignError(
            "Could not align old template %s to template %s (%s_%s). Error: %s"
            % (
                old_template_sequence,
                new_template_sequence,
                mmcif_object.file_id,
                template_chain_id,
                str(e),
            )
        )

    logging.info(
        "Old aligned template: %s\nNew aligned template: %s",
        old_aligned_template,
        new_aligned_template,
    )

    old_to_new_template_mapping = {}
    old_template_index = -1
    new_template_index = -1
    num_same = 0
    for old_template_aa, new_template_aa in zip(
        old_aligned_template, new_aligned_template
    ):
        if old_template_aa != "-":
            old_template_index += 1
        if new_template_aa != "-":
            new_template_index += 1
        if old_template_aa != "-" and new_template_aa != "-":
            old_to_new_template_mapping[old_template_index] = new_template_index
            if old_template_aa == new_template_aa:
                num_same += 1

    # Require at least 90 % sequence identity wrt to the shorter of the sequences.
    if (
        float(num_same)
        / min(len(old_template_sequence), len(new_template_sequence))
        < 0.9
    ):
        raise QueryToTemplateAlignError(
            "Insufficient similarity of the sequence in the database: %s to the "
            "actual sequence in the mmCIF file %s_%s: %s. We require at least "
            "90 %% similarity wrt to the shorter of the sequences. This is not a "
            "problem unless you think this is a template that should be included."
            % (
                old_template_sequence,
                mmcif_object.file_id,
                template_chain_id,
                new_template_sequence,
            )
        )

    new_query_to_template_mapping = {}
    for query_index, old_template_index in old_mapping.items():
        new_query_to_template_mapping[
            query_index
        ] = old_to_new_template_mapping.get(old_template_index, -1)

    new_template_sequence = new_template_sequence.replace("-", "")

    return new_template_sequence, new_query_to_template_mapping


def _check_residue_distances(
    all_positions: np.ndarray,
    all_positions_mask: np.ndarray,
    max_ca_ca_distance: float,
):
    """Checks if the distance between unmasked neighbor residues is ok."""
    ca_position = residue_constants.atom_order["CA"]
    prev_is_unmasked = False
    prev_calpha = None
    for i, (coords, mask) in enumerate(zip(all_positions, all_positions_mask)):
        this_is_unmasked = bool(mask[ca_position])
        if this_is_unmasked:
            this_calpha = coords[ca_position]
            if prev_is_unmasked:
                distance = np.linalg.norm(this_calpha - prev_calpha)
                if distance > max_ca_ca_distance:
                    raise CaDistanceError(
                        "The distance between residues %d and %d is %f > limit %f."
                        % (i, i + 1, distance, max_ca_ca_distance)
                    )
            prev_calpha = this_calpha
        prev_is_unmasked = this_is_unmasked


def _get_atom_positions(
    mmcif_object: mmcif_parsing.MmcifObject,
    auth_chain_id: str,
    max_ca_ca_distance: float,
    _zero_center_positions: bool = False,
) -> Tuple[np.ndarray, np.ndarray]:
    """Gets atom positions and mask from a list of Biopython Residues."""
    coords_with_mask = mmcif_parsing.get_atom_coords(
        mmcif_object=mmcif_object, 
        chain_id=auth_chain_id,
        _zero_center_positions=_zero_center_positions,
    )
    all_atom_positions, all_atom_mask = coords_with_mask
    _check_residue_distances(
        all_atom_positions, all_atom_mask, max_ca_ca_distance
    )
    return all_atom_positions, all_atom_mask


def _extract_template_features(
    mmcif_object: mmcif_parsing.MmcifObject,
    pdb_id: str,
    mapping: Mapping[int, int],
    template_sequence: str,
    query_sequence: str,
    template_chain_id: str,
    kalign_binary_path: str,
    _zero_center_positions: bool = True,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Parses atom positions in the target structure and aligns with the query.

    Atoms for each residue in the template structure are indexed to coincide
    with their corresponding residue in the query sequence, according to the
    alignment mapping provided.

    Args:
        mmcif_object: mmcif_parsing.MmcifObject representing the template.
        pdb_id: PDB code for the template.
        mapping: Dictionary mapping indices in the query sequence to indices in
            the template sequence.
        template_sequence: String describing the amino acid sequence for the
            template protein.
        query_sequence: String describing the amino acid sequence for the query
            protein.
        template_chain_id: String ID describing which chain in the structure proto
            should be used.
        kalign_binary_path: The path to a kalign executable used for template
                realignment.

    Returns:
        A tuple with:
        * A dictionary containing the extra features derived from the template
            protein structure.
        * A warning message if the hit was realigned to the actual mmCIF sequence.
            Otherwise None.

    Raises:
        NoChainsError: If the mmcif object doesn't contain any chains.
        SequenceNotInTemplateError: If the given chain id / sequence can't
            be found in the mmcif object.
        QueryToTemplateAlignError: If the actual template in the mmCIF file
            can't be aligned to the query.
        NoAtomDataInTemplateError: If the mmcif object doesn't contain
            atom positions.
        TemplateAtomMaskAllZerosError: If the mmcif object doesn't have any
            unmasked residues.
    """
    if mmcif_object is None or not mmcif_object.chain_to_seqres:
        raise NoChainsError(
            "No chains in PDB: %s_%s" % (pdb_id, template_chain_id)
        )

    warning = None
    try:
        seqres, chain_id, mapping_offset = _find_template_in_pdb(
            template_chain_id=template_chain_id,
            template_sequence=template_sequence,
            mmcif_object=mmcif_object,
        )
    except SequenceNotInTemplateError:
        # If PDB70 contains a different version of the template, we use the sequence
        # from the mmcif_object.
        chain_id = template_chain_id
        warning = (
            f"The exact sequence {template_sequence} was not found in "
            f"{pdb_id}_{chain_id}. Realigning the template to the actual sequence."
        )
        logging.warning(warning)
        # This throws an exception if it fails to realign the hit.
        seqres, mapping = _realign_pdb_template_to_query(
            old_template_sequence=template_sequence,
            template_chain_id=template_chain_id,
            mmcif_object=mmcif_object,
            old_mapping=mapping,
            kalign_binary_path=kalign_binary_path,
        )
        logging.info(
            "Sequence in %s_%s: %s successfully realigned to %s",
            pdb_id,
            chain_id,
            template_sequence,
            seqres,
        )
        # The template sequence changed.
        template_sequence = seqres
        # No mapping offset, the query is aligned to the actual sequence.
        mapping_offset = 0

    try:
        # Essentially set to infinity - we don't want to reject templates unless
        # they're really really bad.
        all_atom_positions, all_atom_mask = _get_atom_positions(
            mmcif_object, 
            chain_id, 
            max_ca_ca_distance=150.0, 
            _zero_center_positions=_zero_center_positions,
        )
    except (CaDistanceError, KeyError) as ex:
        raise NoAtomDataInTemplateError(
            "Could not get atom data (%s_%s): %s" % (pdb_id, chain_id, str(ex))
        ) from ex

    all_atom_positions = np.split(
        all_atom_positions, all_atom_positions.shape[0]
    )
    all_atom_masks = np.split(all_atom_mask, all_atom_mask.shape[0])

    output_templates_sequence = []
    templates_all_atom_positions = []
    templates_all_atom_masks = []

    for _ in query_sequence:
        # Residues in the query_sequence that are not in the template_sequence:
        templates_all_atom_positions.append(
            np.zeros((residue_constants.atom_type_num, 3))
        )
        templates_all_atom_masks.append(
            np.zeros(residue_constants.atom_type_num)
        )
        output_templates_sequence.append("-")

    for k, v in mapping.items():
        template_index = v + mapping_offset
        templates_all_atom_positions[k] = all_atom_positions[template_index][0]
        templates_all_atom_masks[k] = all_atom_masks[template_index][0]
        output_templates_sequence[k] = template_sequence[v]

    # Alanine (AA with the lowest number of atoms) has 5 atoms (C, CA, CB, N, O).
    if np.sum(templates_all_atom_masks) < 5:
        raise TemplateAtomMaskAllZerosError(
            "Template all atom mask was all zeros: %s_%s. Residue range: %d-%d"
            % (
                pdb_id,
                chain_id,
                min(mapping.values()) + mapping_offset,
                max(mapping.values()) + mapping_offset,
            )
        )

    output_templates_sequence = "".join(output_templates_sequence)

    templates_aatype = residue_constants.sequence_to_onehot(
        output_templates_sequence, residue_constants.HHBLITS_AA_TO_ID
    )

    return (
        {
            "template_all_atom_positions": np.array(
                templates_all_atom_positions
            ),
            "template_all_atom_mask": np.array(templates_all_atom_masks),
            "template_sequence": output_templates_sequence.encode(),
            "template_aatype": np.array(templates_aatype),
            "template_domain_names": f"{pdb_id.lower()}_{chain_id}".encode(),
        },
        warning,
    )


def _build_query_to_hit_index_mapping(
    hit_query_sequence: str,
    hit_sequence: str,
    indices_hit: Sequence[int],
    indices_query: Sequence[int],
    original_query_sequence: str,
) -> Mapping[int, int]:
    """Gets mapping from indices in original query sequence to indices in the hit.

    hit_query_sequence and hit_sequence are two aligned sequences containing gap
    characters. hit_query_sequence contains only the part of the original query
    sequence that matched the hit. When interpreting the indices from the .hhr, we
    need to correct for this to recover a mapping from original query sequence to
    the hit sequence.

    Args:
        hit_query_sequence: The portion of the query sequence that is in the .hhr
            hit
        hit_sequence: The portion of the hit sequence that is in the .hhr
        indices_hit: The indices for each aminoacid relative to the hit sequence
        indices_query: The indices for each aminoacid relative to the original query
            sequence
        original_query_sequence: String describing the original query sequence.

    Returns:
        Dictionary with indices in the original query sequence as keys and indices
        in the hit sequence as values.
    """
    # If the hit is empty (no aligned residues), return empty mapping
    if not hit_query_sequence:
        return {}

    # Remove gaps and find the offset of hit.query relative to original query.
    hhsearch_query_sequence = hit_query_sequence.replace("-", "")
    hit_sequence = hit_sequence.replace("-", "")
    hhsearch_query_offset = original_query_sequence.find(
        hhsearch_query_sequence
    )

    # Index of -1 used for gap characters. Subtract the min index ignoring gaps.
    min_idx = min(x for x in indices_hit if x > -1)
    fixed_indices_hit = [x - min_idx if x > -1 else -1 for x in indices_hit]

    min_idx = min(x for x in indices_query if x > -1)
    fixed_indices_query = [x - min_idx if x > -1 else -1 for x in indices_query]

    # Zip the corrected indices, ignore case where both seqs have gap characters.
    mapping = {}
    for q_i, q_t in zip(fixed_indices_query, fixed_indices_hit):
        if q_t != -1 and q_i != -1:
            if q_t >= len(hit_sequence) or q_i + hhsearch_query_offset >= len(
                original_query_sequence
            ):
                continue
            mapping[q_i + hhsearch_query_offset] = q_t

    return mapping


@dataclasses.dataclass(frozen=True)
class PrefilterResult:
    valid: bool
    error: Optional[str]
    warning: Optional[str]

@dataclasses.dataclass(frozen=True)
class SingleHitResult:
    features: Optional[Mapping[str, Any]]
    error: Optional[str]
    warning: Optional[str]


def _prefilter_hit(
    query_sequence: str,
    hit: parsers.TemplateHit,
    max_template_date: datetime.datetime,
    release_dates: Mapping[str, datetime.datetime],
    obsolete_pdbs: Mapping[str, str],
    strict_error_check: bool = False,
):
    # Fail hard if we can't get the PDB ID and chain name from the hit.
    hit_pdb_code, hit_chain_id = _get_pdb_id_and_chain(hit)

    if hit_pdb_code not in release_dates:
        if hit_pdb_code in obsolete_pdbs:
            hit_pdb_code = obsolete_pdbs[hit_pdb_code]

    # Pass hit_pdb_code since it might have changed due to the pdb being 
    # obsolete.
    try:
        _assess_hhsearch_hit(
            hit=hit,
            hit_pdb_code=hit_pdb_code,
            query_sequence=query_sequence,
            release_dates=release_dates,
            release_date_cutoff=max_template_date,
        )
    except PrefilterError as e:
        hit_name = f"{hit_pdb_code}_{hit_chain_id}"
        msg = f"hit {hit_name} did not pass prefilter: {str(e)}"
        logging.info(msg)
        if strict_error_check and isinstance(e, (DateError, DuplicateError)):
            # In strict mode we treat some prefilter cases as errors.
            return PrefilterResult(valid=False, error=msg, warning=None)

        return PrefilterResult(valid=False, error=None, warning=None)

    return PrefilterResult(valid=True, error=None, warning=None)


@functools.lru_cache(16, typed=False)
def _read_file(path):
    with open(path, 'r') as f:
        file_data = f.read()

    return file_data


def _process_single_hit(
    query_sequence: str,
    hit: parsers.TemplateHit,
    mmcif_dir: str,
    max_template_date: datetime.datetime,
    release_dates: Mapping[str, datetime.datetime],
    obsolete_pdbs: Mapping[str, str],
    kalign_binary_path: str,
    strict_error_check: bool = False,
    _zero_center_positions: bool = True,
    return_mmcif_result: bool = False,
) -> SingleHitResult:
    """Tries to extract template features from a single HHSearch hit."""
    # Fail hard if we can't get the PDB ID and chain name from the hit.
    hit_pdb_code, hit_chain_id = _get_pdb_id_and_chain(hit)

    if hit_pdb_code not in release_dates:
        if hit_pdb_code in obsolete_pdbs:
            hit_pdb_code = obsolete_pdbs[hit_pdb_code]

    mapping = _build_query_to_hit_index_mapping(
        hit.query,
        hit.hit_sequence,
        hit.indices_hit,
        hit.indices_query,
        query_sequence,
    )

    # The mapping is from the query to the actual hit sequence, so we need to
    # remove gaps (which regardless have a missing confidence score).
    template_sequence = hit.hit_sequence.replace("-", "")

    cif_path = os.path.join(mmcif_dir, hit_pdb_code + ".cif")
    logging.info(
        "Reading PDB entry from %s. Query: %s, template: %s",
        cif_path,
        query_sequence,
        template_sequence,
    )

    # Fail if we can't find the mmCIF file.
    cif_string = _read_file(cif_path)

    parsing_result = mmcif_parsing.parse(
        file_id=hit_pdb_code, mmcif_string=cif_string
    )
    if return_mmcif_result:
        return parsing_result

    if parsing_result.mmcif_object is not None:
        hit_release_date = datetime.datetime.strptime(
            parsing_result.mmcif_object.header["release_date"], "%Y-%m-%d"
        )
        if hit_release_date > max_template_date:
            error = "Template %s date (%s) > max template date (%s)." % (
                hit_pdb_code,
                hit_release_date,
                max_template_date,
            )
            if strict_error_check:
                return SingleHitResult(features=None, error=error, warning=None)
            else:
                logging.info(error)
                return SingleHitResult(features=None, error=None, warning=None)

    try:
        features, realign_warning = _extract_template_features(
            mmcif_object=parsing_result.mmcif_object,
            pdb_id=hit_pdb_code,
            mapping=mapping,
            template_sequence=template_sequence,
            query_sequence=query_sequence,
            template_chain_id=hit_chain_id,
            kalign_binary_path=kalign_binary_path,
            _zero_center_positions=_zero_center_positions,
        )

        if hit.sum_probs is None:
            features["template_sum_probs"] = [0]
        else:
            features["template_sum_probs"] = [hit.sum_probs]

        # It is possible there were some errors when parsing the other chains in the
        # mmCIF file, but the template features for the chain we want were still
        # computed. In such case the mmCIF parsing errors are not relevant.
        return SingleHitResult(
            features=features, error=None, warning=realign_warning
        )
    except (
        NoChainsError,
        NoAtomDataInTemplateError,
        TemplateAtomMaskAllZerosError,
    ) as e:
        # These 3 errors indicate missing mmCIF experimental data rather than a
        # problem with the template search, so turn them into warnings.
        warning = (
            "%s_%s (sum_probs: %.2f, rank: %d): feature extracting errors: "
            "%s, mmCIF parsing errors: %s"
            % (
                hit_pdb_code,
                hit_chain_id,
                hit.sum_probs if hit.sum_probs else 0.,
                hit.index,
                str(e),
                parsing_result.errors,
            )
        )
        if strict_error_check:
            return SingleHitResult(features=None, error=warning, warning=None)
        else:
            return SingleHitResult(features=None, error=None, warning=warning)
    except Error as e:
        error = (
            "%s_%s (sum_probs: %.2f, rank: %d): feature extracting errors: "
            "%s, mmCIF parsing errors: %s"
            % (
                hit_pdb_code,
                hit_chain_id,
                hit.sum_probs if hit.sum_probs else 0.,
                hit.index,
                str(e),
                parsing_result.errors,
            )
        )
        return SingleHitResult(features=None, error=error, warning=None)


def get_custom_template_features(
        mmcif_path: str,
        query_sequence: str,
        pdb_id: str,
        chain_id: str,
        kalign_binary_path: str):

    with open(mmcif_path, "r") as mmcif_path:
        cif_string = mmcif_path.read()

    mmcif_parse_result = mmcif_parsing.parse(
        file_id=pdb_id, mmcif_string=cif_string
    )
    template_sequence = mmcif_parse_result.mmcif_object.chain_to_seqres[chain_id]


    mapping = {x:x for x, _ in enumerate(query_sequence)}


    features, warnings = _extract_template_features(
        mmcif_object=mmcif_parse_result.mmcif_object,
        pdb_id=pdb_id,
        mapping=mapping,
        template_sequence=template_sequence,
        query_sequence=query_sequence,
        template_chain_id=chain_id,
        kalign_binary_path=kalign_binary_path,
        _zero_center_positions=True
    )
    features["template_sum_probs"] = [1.0]

    template_features = {}
    for template_feature_name in TEMPLATE_FEATURES:
        template_features[template_feature_name] = []

    for k in template_features:
        template_features[k].append(features[k])

    for name in template_features:
        template_features[name] = np.stack(
            template_features[name], axis=0
        ).astype(TEMPLATE_FEATURES[name])

    return TemplateSearchResult(
        features=template_features, errors=None, warnings=warnings
    )



@dataclasses.dataclass(frozen=True)
class TemplateSearchResult:
    features: Mapping[str, Any]
    errors: Sequence[str]
    warnings: Sequence[str]


class TemplateHitFeaturizer(abc.ABC):
    """An abstract base class for turning template hits to features."""
    def __init__(
        self,
        mmcif_dir: str,
        max_template_date: str,
        max_hits: int,
        kalign_binary_path: str,
        release_dates_path: Optional[str] = None,
        obsolete_pdbs_path: Optional[str] = None,
        strict_error_check: bool = False,
        _shuffle_top_k_prefiltered: Optional[int] = None,
        _zero_center_positions: bool = True,
    ):
        """Initializes the Template Search.

        Args:
            mmcif_dir: Path to a directory with mmCIF structures. Once a template ID
                is found by HHSearch, this directory is used to retrieve the template
                data.
            max_template_date: The maximum date permitted for template structures. No
                template with date higher than this date will be returned. In ISO8601
                date format, YYYY-MM-DD.
            max_hits: The maximum number of templates that will be returned.
            kalign_binary_path: The path to a kalign executable used for template
                realignment.
            release_dates_path: An optional path to a file with a mapping from PDB IDs
                to their release dates. Thanks to this we don't have to redundantly
                parse mmCIF files to get that information.
            obsolete_pdbs_path: An optional path to a file containing a mapping from
                obsolete PDB IDs to the PDB IDs of their replacements.
            strict_error_check: If True, then the following will be treated as errors:
                * If any template date is after the max_template_date.
                * If any template has identical PDB ID to the query.
                * If any template is a duplicate of the query.
                * Any feature computation errors.
        """
        self._mmcif_dir = mmcif_dir
        if not glob.glob(os.path.join(self._mmcif_dir, "*.cif")):
            logging.error("Could not find CIFs in %s", self._mmcif_dir)
            raise ValueError(f"Could not find CIFs in {self._mmcif_dir}")

        try:
            self._max_template_date = datetime.datetime.strptime(
                max_template_date, "%Y-%m-%d"
            )
        except ValueError:
            raise ValueError(
                "max_template_date must be set and have format YYYY-MM-DD."
            )
        self._max_hits = max_hits
        self._kalign_binary_path = kalign_binary_path
        self._strict_error_check = strict_error_check

        if release_dates_path:
            logging.info(
                "Using precomputed release dates %s.", release_dates_path
            )
            self._release_dates = _parse_release_dates(release_dates_path)
        else:
            self._release_dates = {}

        if obsolete_pdbs_path:
            logging.info(
                "Using precomputed obsolete pdbs %s.", obsolete_pdbs_path
            )
            self._obsolete_pdbs = _parse_obsolete(obsolete_pdbs_path)
        else:
            self._obsolete_pdbs = {}

        self._shuffle_top_k_prefiltered = _shuffle_top_k_prefiltered
        self._zero_center_positions = _zero_center_positions

    @abc.abstractmethod
    def get_templates(
        self,
        query_sequence: str,
        hits: Sequence[parsers.TemplateHit]
    ) -> TemplateSearchResult:
        """ Computes the templates for a given query sequence """


class HhsearchHitFeaturizer(TemplateHitFeaturizer):
    def get_templates(
        self,
        query_sequence: str,
        hits: Sequence[parsers.TemplateHit],
    ) -> TemplateSearchResult:
        """Computes the templates for given query sequence (more details above)."""
        logging.info("Searching for template for: %s", query_sequence)

        template_features = {}
        for template_feature_name in TEMPLATE_FEATURES:
            template_features[template_feature_name] = []

        already_seen = set()
        errors = []
        warnings = []

        filtered = []
        for hit in hits:
            prefilter_result = _prefilter_hit(
                query_sequence=query_sequence,
                hit=hit,
                max_template_date=self._max_template_date,
                release_dates=self._release_dates,
                obsolete_pdbs=self._obsolete_pdbs,
                strict_error_check=self._strict_error_check,
            )

            if prefilter_result.error:
                errors.append(prefilter_result.error)

            if prefilter_result.warning:
                warnings.append(prefilter_result.warning)

            if prefilter_result.valid:
                filtered.append(hit)

        filtered = list(
            sorted(filtered, key=lambda x: x.sum_probs, reverse=True)
        )

        idx = list(range(len(filtered)))
        if(self._shuffle_top_k_prefiltered):
            stk = self._shuffle_top_k_prefiltered
            idx[:stk] = np.random.permutation(idx[:stk])

        for i in idx:
            # We got all the templates we wanted, stop processing hits.
            if len(already_seen) >= self._max_hits:
                break

            hit = filtered[i]

            result = _process_single_hit(
                query_sequence=query_sequence,
                hit=hit,
                mmcif_dir=self._mmcif_dir,
                max_template_date=self._max_template_date,
                release_dates=self._release_dates,
                obsolete_pdbs=self._obsolete_pdbs,
                strict_error_check=self._strict_error_check,
                kalign_binary_path=self._kalign_binary_path,
                _zero_center_positions=self._zero_center_positions,
            )

            if result.error:
                errors.append(result.error)

            # There could be an error even if there are some results, e.g. thrown by
            # other unparsable chains in the same mmCIF file.
            if result.warning:
                warnings.append(result.warning)

            if result.features is None:
                logging.info(
                    "Skipped invalid hit %s, error: %s, warning: %s",
                    hit.name,
                    result.error,
                    result.warning,
                )
            else:
                already_seen_key = result.features["template_sequence"]
                if(already_seen_key in already_seen):
                    continue
                already_seen.add(already_seen_key)
                for k in template_features:
                    template_features[k].append(result.features[k])

        if already_seen:
            for name in template_features:
                template_features[name] = np.stack(
                    template_features[name], axis=0
                ).astype(TEMPLATE_FEATURES[name])
        else:
            num_res = len(query_sequence)
            # Construct a default template with all zeros.
            template_features = empty_template_feats(num_res)

        return TemplateSearchResult(
            features=template_features, errors=errors, warnings=warnings
        )


class HmmsearchHitFeaturizer(TemplateHitFeaturizer):
    def get_templates(
        self,
        query_sequence: str,
        hits: Sequence[parsers.TemplateHit]
    ) -> TemplateSearchResult:
        logging.info("Searching for template for: %s", query_sequence)

        template_features = {}
        for template_feature_name in TEMPLATE_FEATURES:
            template_features[template_feature_name] = []

        already_seen = set()
        errors = []
        warnings = []

        # DISCREPANCY: This filtering scheme that saves time
        filtered = []
        for hit in hits:
            prefilter_result = _prefilter_hit(
                query_sequence=query_sequence,
                hit=hit,
                max_template_date=self._max_template_date,
                release_dates=self._release_dates,
                obsolete_pdbs=self._obsolete_pdbs,
                strict_error_check=self._strict_error_check,
            )

            if prefilter_result.error:
                errors.append(prefilter_result.error)

            if prefilter_result.warning:
                warnings.append(prefilter_result.warning)

            if prefilter_result.valid:
                filtered.append(hit)

        filtered = list(
            sorted(
                filtered, key=lambda x: x.sum_probs if x.sum_probs else 0., reverse=True
            )
        )
        idx = list(range(len(filtered)))
        if(self._shuffle_top_k_prefiltered):
            stk = self._shuffle_top_k_prefiltered
            idx[:stk] = np.random.permutation(idx[:stk])

        for i in idx:
            if(len(already_seen) >= self._max_hits):
                break

            hit = filtered[i]

            result = _process_single_hit(
                query_sequence=query_sequence,
                hit=hit,
                mmcif_dir=self._mmcif_dir,
                max_template_date = self._max_template_date,
                release_dates = self._release_dates,
                obsolete_pdbs = self._obsolete_pdbs,
                strict_error_check = self._strict_error_check,
                kalign_binary_path = self._kalign_binary_path
            )

            if result.error:
                errors.append(result.error)

            if result.warning:
                warnings.append(result.warning)

            if result.features is None:
                logging.debug(
                    "Skipped invalid hit %s, error: %s, warning: %s",
                    hit.name, result.error, result.warning,
                )
            else:
                already_seen_key = result.features["template_sequence"]
                if(already_seen_key in already_seen):
                    continue
                # Increment the hit counter, since we got features out of this hit.
                already_seen.add(already_seen_key)
                for k in template_features:
                    template_features[k].append(result.features[k])

        if already_seen:
            for name in template_features:
                template_features[name] = np.stack(
                    template_features[name], axis=0
                ).astype(TEMPLATE_FEATURES[name])
        else:
            num_res = len(query_sequence)
            # Construct a default template with all zeros.
            template_features = empty_template_feats(num_res)

        return TemplateSearchResult(
            features=template_features,
            errors=errors,
            warnings=warnings,
        )
