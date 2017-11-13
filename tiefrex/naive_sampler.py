"""
Implements a generator for basic uniform random sampling of negative items
"""
import sys
import numpy as np
import scipy.sparse as sp
import tensorflow as tf
from tiefrex.constants import *
from tiefrex.data import TrainDataLoader
from tiefrex.utils_sparse import get_row_nz
from typing import Dict, Iterable, Sized
import itertools as it

def batcher(iterable: Sized, n=1):
    """ Generates fixed-size chunks (will not yield last chunk if too small)
    """
    l = len(iterable)
    for ii in range(0, l // n * n, n):
        yield iterable[ii:min(ii + n, l)]


def feed_via_pair(input_pair_d: Dict[str, tf.Tensor],
                  user_feed_d: Dict[str, Iterable],
                  pos_item_feed_d: Dict[str, Iterable],
                  neg_item_feed_d: Dict[str, Iterable],
                  context_feed_d: Dict[str, Iterable],
                  ):
    feed_pair_dict = {
        **{input_pair_d[f'{USER_VAR_TAG}.{feat_name}']: data_in
           for feat_name, data_in in user_feed_d.items()},
        **{input_pair_d[f'{POS_VAR_TAG}.{feat_name}']: data_in
           for feat_name, data_in in pos_item_feed_d.items()},
        **{input_pair_d[f'{NEG_VAR_TAG}.{feat_name}']: data_in
           for feat_name, data_in in neg_item_feed_d.items()},
        **{input_pair_d[f'{CONTEXT_VAR_TAG}.{feat_name}']: data_in
           for feat_name, data_in in context_feed_d.items()},
    }
    return feed_pair_dict


def feed_via_inds(inds_batch, cols, codes_arr, num_arr, num_key):
    """
    :param inds_batch: indices of the batch to slice
    :param cols: {user|item}_cols 
    :param codes_arr: categorical codes array 
        [n_total_samples x n_categorical_features]
    :param num_arr: numerical features array
        [n_total_samples x n_numerical_features]
    :param num_key: numerical features key
    :return: 
    """
    if codes_arr is None:
        return {}
    d = dict(zip(cols, codes_arr[inds_batch, :].T))
    if num_arr is not None and num_key is not None:
        d[num_key] = num_arr[inds_batch, :]
    return d


class PairSampler(object):
    def __init__(self,
                 train_data_loader: TrainDataLoader,
                 input_pair_d: Dict[str, tf.Tensor],
                 batch_size: int=1024,
                 shuffle: bool=True,
                 n_epochs: int=-1,
                 uniform_users: bool=False,
                 method: str='uniform',
                 model=None,
                 seed: int=0,
                 ):
        """
        :param train_data_loader: tiefrex.core.TrainDataLoader
        :param input_pair_d: dictionary of placeholders keyed by name
        :param batch_size: batch size
        :param shuffle: if `True` batches will be sampled from a shuffled index
        :param n_epochs: number of epochs until `StopIteration`
        :param uniform_users: if `True` sample by user
            rather than by positive interaction
            (optimize all users equally rather than weighing more active users)
        :param method: negative sampling method
        :param model: network object that implements a forward method
            for adaptive sampling
        """
        self.seed = seed
        np.random.seed(self.seed)

        interactions_df = train_data_loader.interactions_df
        user_col = train_data_loader.user_col
        item_col = train_data_loader.item_col

        # Index alignment
        user_feats_codes_df = train_data_loader.user_feats_codes_df.loc[
            train_data_loader.cats_d[train_data_loader.user_col]]
        item_feats_codes_df = train_data_loader.item_feats_codes_df.loc[
            train_data_loader.cats_d[train_data_loader.item_col]]

        # TODO: some switch for context existence
        # context features are already aligned with `interaction_df`
        #   by construction
        context_feat_codes_df = train_data_loader.context_feats_codes_df

        # Grab underlying numerical feature array(s)
        self.user_num_feats_arr = None
        self.item_num_feats_arr = None
        if FType.NUM in train_data_loader.user_feats_d:
            self.user_num_feats_arr = train_data_loader.\
                user_feats_d[FType.NUM].loc[train_data_loader.cats_d[
                    train_data_loader.user_col]].values
        if FType.NUM in train_data_loader.item_feats_d:
            self.item_num_feats_arr = train_data_loader.\
                item_feats_d[FType.NUM].loc[train_data_loader.cats_d[
                    train_data_loader.item_col]].values
        # TODO: NUM not supported for context right now

        self.method = method
        self.get_negs = {
            'uniform': self.sample_uniform,
            'uniform_verified': self.sample_uniform_verified,
            'adaptive': self.sample_adaptive,
        }[self.method]

        self.n_epochs = n_epochs if n_epochs >= 0 else sys.maxsize
        self.batch_size = batch_size
        self.shuffle = shuffle

        self.uniform_users = uniform_users

        self.input_pair_d = input_pair_d
        self._model = model

        # Upfront processing
        self.n_users = len(interactions_df[user_col].cat.categories)
        self.n_items = len(interactions_df[item_col].cat.categories)
        self.xn_coo = sp.coo_matrix(
            (np.ones(len(interactions_df), dtype=bool),
             (interactions_df[user_col].cat.codes,
              interactions_df[item_col].cat.codes)),
            shape=(self.n_users, self.n_items), dtype=bool)

        if self.method in {'uniform_verified', 'adaptive'} \
                or self.uniform_users:
            self.xn_csr = self.xn_coo.tocsr()
        else:
            self.xn_csr = None

        if self.uniform_users:
            # index for each user
            self.shuffle_inds = np.arange(self.n_users)
        else:
            # index for each pos interaction
            self.shuffle_inds = np.arange(len(self.xn_coo.data))

        self.user_feats_codes_arr = user_feats_codes_df.values
        self.item_feats_codes_arr = item_feats_codes_df.values
        self.context_feats_codes_arr = context_feat_codes_df.values \
            if context_feat_codes_df is not None else None
        self.user_cols = user_feats_codes_df.columns
        self.item_cols = item_feats_codes_df.columns
        self.context_cols = context_feat_codes_df.columns \
            if context_feat_codes_df is not None else []

        # Some more crap for the more complex strats
        if self.method == 'adaptive':
            self.max_sampled = 32  # for WARP
            self.pos_fwd_d = self._model.get_fwd_dict(
                batch_size=self.batch_size)
            self.pos_fwd_op = self._model.forward(self.pos_fwd_d)
            self.neg_fwd_d = self._model.get_fwd_dict()
            self.neg_fwd_op = self._model.forward(self.neg_fwd_d)

        self.sess = None

    def __iter__(self):
        if self.uniform_users:
            return self.iter_by_user()
        else:
            return self.iter_by_xn()

    def sample_uniform(self, **_):
        return np.random.randint(self.n_items, size=self.batch_size)

    def sample_uniform_verified(self, user_inds_batch, **_):
        """ Ensures that the neg samples are not known positives
        Consider just using `sample_uniform` as this can be ~20x slower
        """
        neg_item_inds_batch = []
        for user_ind in user_inds_batch:
            user_pos_item_inds = get_row_nz(self.xn_csr, user_ind)
            neg_item_ind = np.random.randint(self.n_items)
            while neg_item_ind in user_pos_item_inds:
                neg_item_ind = np.random.randint(self.n_items)
            neg_item_inds_batch.append(neg_item_ind)
        return neg_item_inds_batch

    def sample_adaptive(self, user_inds_batch, pos_item_inds_batch,
                        use_first_violation=False,
                        nonpos_verification=False,  # TODO
                        ):
        """
        Uses the forward prediction of `self._model` to sample the 
            first violating negative
        Note: If WARP, we also return the number of samples we passed through
         TODO: need to handle this return signature somehow
         
         use_first_violation: if `True`, use the first violation,
            else, we just use the worst offender
        nonpos_verification: if `True`, negatives will be verified as non-pos
        """
        batch_size = len(user_inds_batch)  # NOT max_sampled

        pos_fwd_dict = self.fwd_dicter_via_inds(
            user_inds_batch, pos_item_inds_batch, self.pos_fwd_d)
        pos_scores = self.sess.run(self.pos_fwd_op, feed_dict=pos_fwd_dict)

        neg_item_inds = np.random.randint(
            self.n_items, size=[batch_size, self.max_sampled])

        neg_cand_fwd_dict = self.fwd_dicter_via_inds(
            np.tile(user_inds_batch[:, None], self.max_sampled).flatten(),
            neg_item_inds.flatten(),
            self.neg_fwd_d)

        # These have shape = (batch_size, max_sampled)
        neg_cand_scores = self.sess.run(
            self.neg_fwd_op, feed_dict=neg_cand_fwd_dict
        ).reshape([-1, self.max_sampled])
        pos_scores_tile = np.tile(pos_scores[:, None], self.max_sampled)

        if use_first_violation:
            violations = (neg_cand_scores > pos_scores_tile - 1)
            # Get index of the first violation
            first_violator_inds = np.argmax(violations, axis=1)

            # For the users with no violations, set first violation to last ind
            first_violator_inds[~violations[
                range(batch_size), first_violator_inds]
            ] = self.max_sampled - 1

            neg_item_inds_batch = neg_item_inds[
                range(len(neg_item_inds)), first_violator_inds
            ]
        else:
            # Get the worst offender
            # TODO: maybe do the non-pos verification in this case
            neg_item_inds_batch = neg_item_inds[
                range(batch_size), np.argmax(neg_cand_scores, axis=1)
            ]

        return neg_item_inds_batch

    def user_feed_via_inds(self, user_inds_batch):
        return feed_via_inds(user_inds_batch,
                             self.user_cols,
                             self.user_feats_codes_arr,
                             self.user_num_feats_arr,
                             num_key='user_num_feats',
                             )

    def item_feed_via_inds(self, item_inds_batch):
        return feed_via_inds(item_inds_batch,
                             self.item_cols,
                             self.item_feats_codes_arr,
                             self.item_num_feats_arr,
                             num_key='item_num_feats',
                             )

    def context_feed_via_inds(self, inds_batch):
        return feed_via_inds(inds_batch,
                             self.context_cols,
                             self.context_feats_codes_arr,
                             num_arr=None,
                             num_key=None,
                             )

    def iter_by_xn(self):
        # The feed dict generator itself
        # Note: can implement __next__ as well
        #   if we want book-keeping state info to be kept
        for i in range(self.n_epochs):
            if self.shuffle:
                np.random.shuffle(self.shuffle_inds)
            # TODO: problem if less inds than batch_size
            inds_batcher = batcher(self.shuffle_inds, n=self.batch_size)
            for inds_batch in inds_batcher:
                user_inds_batch = self.xn_coo.row[inds_batch]
                pos_item_inds_batch = self.xn_coo.col[inds_batch]
                neg_item_inds_batch = self.get_negs(
                    user_inds_batch=user_inds_batch,
                    pos_item_inds_batch=pos_item_inds_batch,)

                user_feed_d = self.user_feed_via_inds(user_inds_batch)
                pos_item_feed_d = self.item_feed_via_inds(pos_item_inds_batch)
                neg_item_feed_d = self.item_feed_via_inds(neg_item_inds_batch)

                context_feed_d = self.context_feed_via_inds(inds_batch)

                feed_pair_dict = feed_via_pair(
                    self.input_pair_d,
                    user_feed_d,
                    pos_item_feed_d, neg_item_feed_d,
                    context_feed_d
                )
                yield feed_pair_dict

    def iter_by_user(self):
        # The feed dict generator itself
        # Note: can implement __next__ as well
        #   if we want book-keeping state info to be kept
        for i in range(self.n_epochs):
            if self.shuffle:
                np.random.shuffle(self.shuffle_inds)
            # TODO: problem if less inds than batch_size
            inds_batcher = batcher(self.shuffle_inds, n=self.batch_size)
            for inds_batch in inds_batcher:
                # TODO: WIP>>>
                user_inds_batch = inds_batch
                pos_l = []

                for user_ind in user_inds_batch:
                    user_pos_item_inds = get_row_nz(self.xn_csr, user_ind)
                    # `random.choice` slow
                    user_pos_item = user_pos_item_inds[np.random.randint(
                        len(user_pos_item_inds))]
                    pos_l.append(user_pos_item)
                # Select random known pos for user
                # pos_item_inds_batch = self.xn_coo.col[inds_batch]
                pos_item_inds_batch = np.array(pos_l)
                neg_item_inds_batch = self.get_negs(
                    user_inds_batch=user_inds_batch,
                    pos_item_inds_batch=pos_item_inds_batch,)

                user_feed_d = self.user_feed_via_inds(user_inds_batch)
                pos_item_feed_d = self.item_feed_via_inds(pos_item_inds_batch)
                neg_item_feed_d = self.item_feed_via_inds(neg_item_inds_batch)

                # TODO: fix context feed
                feed_pair_dict = feed_via_pair(
                    self.input_pair_d,
                    user_feed_d,
                    pos_item_feed_d, neg_item_feed_d,
                    context_feed_d={},
                )
                yield feed_pair_dict

    def fwd_dicter_via_inds(self, user_inds, item_inds, fwd_d):
        """
        :param user_inds: Can be a single user ind or an iterable of user inds
            If a single user ind is provided, it will be repeated 
                for each item in `item_inds`
        :param item_inds: Iterable of item inds
        :param fwd_d: forward feed dict template
        """
        if not hasattr(user_inds, '__iter__'):
            user_inds = [user_inds] * len(item_inds)
        user_feed_d = dict(zip(self.user_cols,
                               self.user_feats_codes_arr[user_inds, :].T))
        item_feed_d = dict(zip(self.item_cols,
                               self.item_feats_codes_arr[item_inds, :].T))
        feed_fwd_dict = {
            **{fwd_d[f'{feat_name}']: data_in
               for feat_name, data_in in user_feed_d.items()},
            **{fwd_d[f'{feat_name}']: data_in
               for feat_name, data_in in item_feed_d.items()},
        }
        return feed_fwd_dict
