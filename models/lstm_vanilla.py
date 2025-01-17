import tensorflow as tf
from tensorflow.python.ops import tensor_array_ops, control_flow_ops
from utils.ops import *


def generator(x_real, temperature, vocab_size, batch_size, seq_len, gen_emb_dim, mem_slots, head_size, num_heads,
              hidden_dim, start_token):
    start_tokens = tf.constant([start_token] * batch_size, dtype=tf.int32)

    # build LSTM unit
    g_embeddings = tf.get_variable('g_emb', shape=[vocab_size, gen_emb_dim],
                                   initializer=create_linear_initializer(vocab_size))
    gen_mem = create_recurrent_unit(emb_dim=gen_emb_dim, hidden_dim=hidden_dim)
    g_output_unit = create_lstm_output_unit(hidden_dim, vocab_size)

    # Initial states
    h0 = tf.zeros([batch_size, hidden_dim])
    init_states = tf.stack([h0, h0])

    # ---------- generate tokens and approximated one-hot results (Adversarial) ---------
    gen_o = tensor_array_ops.TensorArray(dtype=tf.float32, size=seq_len, dynamic_size=False, infer_shape=True)
    gen_x = tensor_array_ops.TensorArray(dtype=tf.int32, size=seq_len, dynamic_size=False, infer_shape=True)
    gen_x_onehot_adv = tensor_array_ops.TensorArray(dtype=tf.float32, size=seq_len, dynamic_size=False,
                                                    infer_shape=True)  # generator output (relaxed of gen_x)

    # the generator recurrent module used for adversarial training
    def _gen_recurrence(i, x_t, h_tm1, gen_o, gen_x, gen_x_onehot_adv):
        h_t = gen_mem(x_t, h_tm1)  # hidden_memory_tuple
        o_t = g_output_unit(h_t)  # batch x vocab, logits not probs
        gumbel_t = add_gumbel(o_t)
        next_token = tf.stop_gradient(tf.argmax(gumbel_t, axis=1, output_type=tf.int32))
        next_token_onehot = tf.one_hot(next_token, vocab_size, 1.0, 0.0)

        x_onehot_appr = tf.nn.softmax(tf.multiply(gumbel_t, temperature))  # one-hot-like, [batch_size x vocab_size]

        # x_tp1 = tf.matmul(x_onehot_appr, g_embeddings)  # approximated embeddings, [batch_size x emb_dim]
        x_tp1 = tf.nn.embedding_lookup(g_embeddings, next_token)  # embeddings, [batch_size x emb_dim]

        gen_o = gen_o.write(i, tf.reduce_sum(tf.multiply(next_token_onehot, x_onehot_appr), 1))  # [batch_size], prob
        gen_x = gen_x.write(i, next_token)  # indices, [batch_size]

        gen_x_onehot_adv = gen_x_onehot_adv.write(i, x_onehot_appr)

        return i + 1, x_tp1, h_t, gen_o, gen_x, gen_x_onehot_adv

    # build a graph for outputting sequential tokens
    _, _, _, gen_o, gen_x, gen_x_onehot_adv = control_flow_ops.while_loop(
        cond=lambda i, _1, _2, _3, _4, _5: i < seq_len,
        body=_gen_recurrence,
        loop_vars=(tf.constant(0, dtype=tf.int32), tf.nn.embedding_lookup(g_embeddings, start_tokens),
                   init_states, gen_o, gen_x, gen_x_onehot_adv))

    gen_o = tf.transpose(gen_o.stack(), perm=[1, 0])  # batch_size x seq_len
    gen_x = tf.transpose(gen_x.stack(), perm=[1, 0])  # batch_size x seq_len

    gen_x_onehot_adv = tf.transpose(gen_x_onehot_adv.stack(), perm=[1, 0, 2])  # batch_size x seq_len x vocab_size

    # ----------- pre-training for generator -----------------
    x_emb = tf.transpose(tf.nn.embedding_lookup(g_embeddings, x_real), perm=[1, 0, 2])  # seq_len x batch_size x emb_dim
    g_predictions = tensor_array_ops.TensorArray(dtype=tf.float32, size=seq_len, dynamic_size=False, infer_shape=True)

    ta_emb_x = tensor_array_ops.TensorArray(dtype=tf.float32, size=seq_len)
    ta_emb_x = ta_emb_x.unstack(x_emb)

    # the generator recurrent moddule used for pre-training
    def _pretrain_recurrence(i, x_t, h_tm1, g_predictions):
        h_t = gen_mem(x_t, h_tm1)
        o_t = g_output_unit(h_t)
        g_predictions = g_predictions.write(i, tf.nn.softmax(o_t))  # batch_size x vocab_size
        x_tp1 = ta_emb_x.read(i)
        return i + 1, x_tp1, h_t, g_predictions

    # build a graph for outputting sequential tokens
    _, _, _, g_predictions = control_flow_ops.while_loop(
        cond=lambda i, _1, _2, _3: i < seq_len,
        body=_pretrain_recurrence,
        loop_vars=(tf.constant(0, dtype=tf.int32), tf.nn.embedding_lookup(g_embeddings, start_tokens),
                   init_states, g_predictions))

    g_predictions = tf.transpose(g_predictions.stack(),
                                 perm=[1, 0, 2])  # batch_size x seq_length x vocab_size

    # pre-training loss
    pretrain_loss = -tf.reduce_sum(
        tf.one_hot(tf.to_int32(tf.reshape(x_real, [-1])), vocab_size, 1.0, 0.0) * tf.log(
            tf.clip_by_value(tf.reshape(g_predictions, [-1, vocab_size]), 1e-20, 1.0)
        )
    ) / (seq_len * batch_size)

    return gen_x_onehot_adv, gen_x, pretrain_loss, gen_o


def discriminator(x_onehot, batch_size, seq_len, vocab_size, dis_emb_dim, num_rep, sn):
    emb_dim_single = int(dis_emb_dim / num_rep)
    assert isinstance(emb_dim_single, int) and emb_dim_single > 0

    filter_sizes = [2, 3, 4, 5]
    num_filters = [300, 300, 300, 300]
    dropout_keep_prob = 0.75

    d_embeddings = tf.get_variable('d_emb', shape=[vocab_size, dis_emb_dim],
                                   initializer=create_linear_initializer(vocab_size))
    input_x_re = tf.reshape(x_onehot, [-1, vocab_size])
    emb_x_re = tf.matmul(input_x_re, d_embeddings)
    emb_x = tf.reshape(emb_x_re, [batch_size, seq_len, dis_emb_dim])  # batch_size x seq_len x dis_emb_dim

    emb_x_expanded = tf.expand_dims(emb_x, -1)  # batch_size x seq_len x dis_emb_dim x 1
    print('shape of emb_x_expanded: {}'.format(emb_x_expanded.get_shape().as_list()))

    # Create a convolution + maxpool layer for each filter size
    pooled_outputs = []
    for filter_size, num_filter in zip(filter_sizes, num_filters):
        conv = conv2d(emb_x_expanded, num_filter, k_h=filter_size, k_w=emb_dim_single,
                      d_h=1, d_w=emb_dim_single, sn=sn, stddev=None, padding='VALID',
                      scope="conv-%s" % filter_size)  # batch_size x (seq_len-k_h+1) x num_rep x num_filter
        out = tf.nn.relu(conv, name="relu")
        pooled = tf.nn.max_pool(out, ksize=[1, seq_len - filter_size + 1, 1, 1],
                                strides=[1, 1, 1, 1], padding='VALID',
                                name="pool")  # batch_size x 1 x num_rep x num_filter
        pooled_outputs.append(pooled)

    # Combine all the pooled features
    num_filters_total = sum(num_filters)
    h_pool = tf.concat(pooled_outputs, 3)  # batch_size x 1 x num_rep x num_filters_total
    print('shape of h_pool: {}'.format(h_pool.get_shape().as_list()))
    h_pool_flat = tf.reshape(h_pool, [-1, num_filters_total])

    # Add highway
    h_highway = highway(h_pool_flat, h_pool_flat.get_shape()[1], 1, 0)  # (batch_size*num_rep) x num_filters_total

    # Add dropout
    h_drop = tf.nn.dropout(h_highway, dropout_keep_prob, name='dropout')

    # fc
    fc_out = linear(h_drop, output_size=100, use_bias=True, sn=sn, scope='fc')
    logits = linear(fc_out, output_size=1, use_bias=True, sn=sn, scope='logits')
    logits = tf.squeeze(logits, -1)  # batch_size*num_rep

    print("shape of D logits: {}".format(logits.get_shape().as_list()))

    return logits


def create_recurrent_unit(emb_dim, hidden_dim):
    # Weights and Bias for input and hidden tensor
    Wi = tf.get_variable('Wi', shape=[emb_dim, hidden_dim], initializer=create_linear_initializer(emb_dim))
    Ui = tf.get_variable('Ui', shape=[hidden_dim, hidden_dim], initializer=create_linear_initializer(hidden_dim))
    bi = tf.get_variable('bi', shape=[hidden_dim], initializer=create_bias_initializer())

    Wf = tf.get_variable('Wf', shape=[emb_dim, hidden_dim], initializer=create_linear_initializer(emb_dim))
    Uf = tf.get_variable('Uf', shape=[hidden_dim, hidden_dim], initializer=create_linear_initializer(hidden_dim))
    bf = tf.get_variable('bf', shape=[hidden_dim], initializer=create_bias_initializer())

    Wog = tf.get_variable('Wog', shape=[emb_dim, hidden_dim], initializer=create_linear_initializer(emb_dim))
    Uog = tf.get_variable('Uog', shape=[hidden_dim, hidden_dim], initializer=create_linear_initializer(hidden_dim))
    bog = tf.get_variable('bog', shape=[hidden_dim], initializer=create_bias_initializer())

    Wc = tf.get_variable('Wc', shape=[emb_dim, hidden_dim], initializer=create_linear_initializer(emb_dim))
    Uc = tf.get_variable('Uc', shape=[hidden_dim, hidden_dim], initializer=create_linear_initializer(hidden_dim))
    bc = tf.get_variable('bc', shape=[hidden_dim], initializer=create_bias_initializer())

    def unit(x, hidden_memory_tm1):
        previous_hidden_state, c_prev = tf.unstack(hidden_memory_tm1)

        # Input Gate
        i = tf.sigmoid(
            tf.matmul(x, Wi) +
            tf.matmul(previous_hidden_state, Ui) + bi
        )

        # Forget Gate
        f = tf.sigmoid(
            tf.matmul(x, Wf) +
            tf.matmul(previous_hidden_state, Uf) + bf
        )

        # Output Gate
        o = tf.sigmoid(
            tf.matmul(x, Wog) +
            tf.matmul(previous_hidden_state, Uog) + bog
        )

        # New Memory Cell
        c_ = tf.nn.tanh(
            tf.matmul(x, Wc) +
            tf.matmul(previous_hidden_state, Uc) + bc
        )

        # Final Memory cell
        c = f * c_prev + i * c_

        # Current Hidden state
        current_hidden_state = o * tf.nn.tanh(c)

        return tf.stack([current_hidden_state, c])

    return unit


def create_lstm_output_unit(hidden_dim, vocab_size):
    Wo = tf.get_variable('Wo', shape=[hidden_dim, vocab_size], initializer=create_linear_initializer(hidden_dim))
    bo = tf.get_variable('bo', shape=[vocab_size], initializer=create_bias_initializer())

    def unit(hidden_memory_tuple):
        hidden_state, c_prev = tf.unstack(hidden_memory_tuple)
        logits = tf.matmul(hidden_state, Wo) + bo
        return logits

    return unit
