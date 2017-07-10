import tensorflow as tf
import numpy as np
from lib_cerebro_py.log import logger
from tiefrex.data import load_simple_warm_cats, TrainDataLoader
from tiefrex.core import fwd_dict_via_ftypemeta
from tiefrex.constants import FType


def items_pred_dicter(user_id, item_ids,
                      user_cat_codes_df, item_cat_codes_df,
                      user_num_feats_df, item_num_feats_df,
                      input_fwd_d,
                      ):
    # todo: a little redundancy with the dicters in tiefrex.core
    """
    Creates feeds for forward prediction for a single user    
    Note: This does not batch within the list of items passed in
        thus, it could be a problem with huge number of items

    """
    n_items = len(item_ids)

    user_feed_d = user_cat_codes_df.loc[[user_id]].to_dict(orient='list')
    item_feed_d = item_cat_codes_df.loc[item_ids].to_dict(orient='list')

    # Add numerical feature if present
    if user_num_feats_df is not None:
        user_feed_d.update(
            {'user_num_feats': user_num_feats_df.loc[[user_id]].values})
    if item_num_feats_df is not None:
        item_feed_d.update(
            {'item_num_feats': item_num_feats_df.loc[item_ids].values})

    feed_fwd_dict = {
        **{input_fwd_d[f'{feat_name}']: data_in * n_items
           for feat_name, data_in in user_feed_d.items()},
        **{input_fwd_d[f'{feat_name}']: data_in
           for feat_name, data_in in item_feed_d.items()},
    }
    return feed_fwd_dict


def items_pred_dicter_gen(user_ids, item_ids,
                          user_cat_codes_df, item_cat_codes_df,
                          user_num_feats_df, item_num_feats_df,
                          input_fwd_d, ):
    """Generates feeds for forward prediction for many users
    each batch will be all items for a single user
    """
    for user_id in user_ids:
        yield user_id, items_pred_dicter(user_id, item_ids,
                                         user_cat_codes_df, item_cat_codes_df,
                                         user_num_feats_df, item_num_feats_df,
                                         input_fwd_d)


def make_metrics_ops(fwd_op, input_fwd_d):
    """
    :param fwd_op: forward inference operation (typically `model.forward`) 
    :param input_fwd_d: dict of forward placeholders
    :return: 
    """
    with tf.name_scope('placeholders_eval'):
        ph_d = {
            'y_true_ph': tf.placeholder('int64'),
            'y_true_bool_ph': tf.placeholder('bool'),
        }

    # Define our metrics: MAP@10 and AUC (hardcoded for now)
    k = 10
    with tf.name_scope('stream_metrics'):
        mapk, update_op_mapk = tf.contrib.metrics.streaming_sparse_average_precision_at_k(
            tf.expand_dims(fwd_op(input_fwd_d), 0),
            ph_d['y_true_ph'], k=k)

        auc, update_op_auc = tf.contrib.metrics.streaming_auc(
            tf.expand_dims(tf.sigmoid(fwd_op(input_fwd_d)), 0),
            ph_d['y_true_bool_ph'])
    stream_vars = [i for i in tf.local_variables() if i.name.split('/')[0] == 'stream_metrics']
    reset_metrics_op = [tf.variables_initializer(stream_vars)]

    metric_ops_d = {
        'mapk': (mapk, update_op_mapk),
        'auc': (auc, update_op_auc),
    }
    return metric_ops_d, reset_metrics_op, ph_d


def eval_things(sess,
                interactions_df,
                user_col, item_col,
                user_ids_val, item_ids,
                user_cat_codes_df, item_cat_codes_df,
                user_num_feats_df, item_num_feats_df,
                input_fwd_d,
                metric_ops_d, reset_metrics_op, eval_ph_d,
                n_users_eval=-1,
                summary_writer=None, step=None,
                ):
    """
    :param sess: tensorflow session
    :param interactions_df: dataframe of positive interactions
    :param user_col: name of user column
    :param item_col: name of item column
    :param user_ids_val: user_ids to evaluate
    :param item_ids: item_ids in catalog to consider
    :param user_cat_codes_df: user feature codes
    :param item_cat_codes_df: item feature codes
    :param user_num_feats_df: user numerical features
    :param item_num_feats_df: item numerical features
    :param input_fwd_d: forward feed dictionary
    :param metric_ops_d: dictionary of metric operations
    :param reset_metrics_op: reset operation for streaming metrics
    :param eval_ph_d: dictionary of placeholder for evaluation
    :param n_users_eval: max number of users to evaluate
        (if evaluation is too slow to consider all users in `user_ids_val`)
    :param summary_writer: optional summary writer
    :param step: optional global step for summary
    """
    sess.run(tf.local_variables_initializer())
    sess.run(reset_metrics_op)
    # use the same users for every eval step
    pred_feeder_gen = items_pred_dicter_gen(
        user_ids_val, item_ids,
        user_cat_codes_df, item_cat_codes_df,
        user_num_feats_df, item_num_feats_df,
        input_fwd_d)
    if n_users_eval < 0:
        n_users_eval = len(user_ids_val)
    else:
        n_users_eval = min(n_users_eval, len(user_ids_val))
    for ii in range(n_users_eval):
        try:
            user_id, cur_user_fwd_dict = next(pred_feeder_gen)
        except StopIteration:
            break

        y_true = interactions_df.loc[interactions_df[user_col]
                                     == user_id][item_col].cat.codes.values
        y_true_bool = np.zeros(len(item_ids), dtype=bool)
        y_true_bool[y_true] = True

        sess.run([tup[1] for tup in metric_ops_d.values()], feed_dict={
            **cur_user_fwd_dict,
            **{eval_ph_d['y_true_ph']: y_true[None, :],
               eval_ph_d['y_true_bool_ph']: y_true_bool[None, :],
               }})

    for m, m_tup in metric_ops_d.items():
        metric_score = sess.run(m_tup[0])
        metric_val_summary = tf.Summary(value=[
            tf.Summary.Value(tag=f'{m}_val',
                             simple_value=metric_score)])
        logger.info(f'(val){m} = {metric_score}')
        if summary_writer is not None:
            summary_writer.add_summary(metric_val_summary, step)


class Validator(object):
    def __init__(self, config, train_data_loader: TrainDataLoader):
        self.cats_d, self.user_cat_codes_df, self.item_cat_codes_df = \
            train_data_loader.export_data_encoding()
        self.user_num_feats_df = train_data_loader.user_num_feats_df
        self.item_num_feats_df = train_data_loader.item_num_feats_df
        self.num_meta = train_data_loader.num_meta

        interactions_val = config.get('eval_interactions')

        self.user_col_val = interactions_val.user_col
        self.item_col_val = interactions_val.item_col

        self.interactions_val_df = load_simple_warm_cats(
            interactions_val,
            self.cats_d[self.user_col_val], self.cats_d[self.item_col_val],
        )

        self.item_ids = self.cats_d[self.item_col_val]
        self.user_ids_val = self.interactions_val_df[self.user_col_val].unique()
        np.random.shuffle(self.user_ids_val)
        structs = {FType.CAT: list(self.cats_d.keys()),
                   FType.NUM: list(self.num_meta.items()),
                   }
        with tf.name_scope('placeholders'):
            self.input_fwd_d = fwd_dict_via_ftypemeta(
                structs, batch_size=len(self.item_ids))

    def ops(self, model):
        # Eval ops
        # Define our metrics: MAP@10 and AUC
        self.model = model

        self.metric_ops_d, self.reset_metrics_op, self.eval_ph_d = make_metrics_ops(
            self.model.forward, self.input_fwd_d)

    def run_val(self, sess, summary_writer, step):
        eval_things(
            sess,
            self.interactions_val_df,
            self.user_col_val, self.item_col_val,
            self.user_ids_val, self.item_ids,
            self.user_cat_codes_df, self.item_cat_codes_df,
            self.user_num_feats_df, self.item_num_feats_df,
            self.input_fwd_d,
            self.metric_ops_d, self.reset_metrics_op, self.eval_ph_d,
            n_users_eval=20,
            summary_writer=summary_writer, step=step,
        )