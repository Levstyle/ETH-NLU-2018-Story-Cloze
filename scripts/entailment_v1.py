"""
Credits to
- A large annotated corpus for learning natural language inference,
    _Samuel R. Bowman, Gabor Angeli, Christopher Potts, and Christopher D. Manning_,
    https://nlp.stanford.edu/pubs/snli_paper.pdf.
- AllenNLP for Elmo Embeddings: Deep contextualized word representations
    _Matthew E. Peters, Mark Neumann, Mohit Iyyer, Matt Gardner, Christopher Clark, Kenton Lee, Luke Zettlemoyer_,
    https://arxiv.org/abs/1802.05365.
- Jacob Zweig for Elmo embedding import code from
https://towardsdatascience.com/elmo-embeddings-in-keras-with-tensorflow-hub-7eb6f0145440.
"""
import datetime
import os
import random

import keras
from keras import backend as K
from keras.layers import BatchNormalization
import tensorflow as tf
import tensorflow_hub as hub
import numpy as np

from utils import Dataloader
from utils import SNLIDataloader
from nltk import word_tokenize
from scripts import DefaultScript


class Script(DefaultScript):
    slug = 'entailment_v1'

    def train(self):
        main(self.config)


def preprocess_fn(line):
    # label = [entailment, neutral, contradiction]
    label = 1
    if line['gold_label'] == 'contradiction':
        label = 0
    sentence1 = line['sentence1']
    sentence2 = line['sentence2']
    output = [label, sentence1, sentence2]
    return output


def output_fn(_, batch):
    batch = np.array(batch, dtype=object)
    return [batch[:, 1], batch[:, 2]], np.array(list(batch[:, 0]))


def output_fn_test(data):
    batch = np.array(data.batch)
    ref_sentences = []
    input_sentences = []
    label = []
    for b in batch:
        sentence = " ".join(b[3])
        if random.random() > 0.5:
            input_sentences.append(" ".join(b[4]))
            label.append(2 - int(b[6][0]))
        else:
            input_sentences.append(" ".join(b[5]))
            label.append(int(b[6][0]) - 1)
        ref_sentences.append(sentence)
    ref_sentences, input_sentences = np.array(ref_sentences, dtype=object), np.array(input_sentences, dtype=object)
    return [ref_sentences, input_sentences], np.array(label)


class ElmoEmbedding:
    def __init__(self, elmo_model):
        self.elmo_model = elmo_model
        self.__name__ = "elmo_embeddings"

    def __call__(self, x):
        return self.elmo_model(tf.squeeze(tf.cast(x, tf.string)), signature="default", as_dict=True)[
            "elmo"]


class IntraAttn:
    def __init__(self, attn_layer_1, attn_layer_2):
        self.attn_layer_2 = attn_layer_2
        self.attn_layer_1 = attn_layer_1
        self.__name__ = 'intra_attn'

    def __call__(self, x):
        weights = keras.layers.Dropout(0.2)(self.attn_layer_1(x))
        weights = BatchNormalization()(keras.layers.Dropout(0.2)(self.attn_layer_2(weights)))
        attn_weights = K.batch_dot(K.permute_dimensions(weights, (0, 2, 1)), weights, axes=(1, 2))
        attn = K.softmax(attn_weights, axis=1)
        # batch_size x lB x 1024
        attn = K.batch_dot(K.permute_dimensions(attn, (0, 2, 1)), x, axes=(2, 1))
        return attn


class AlphaFN:
    def __init__(self, attend_layer_1, attend_layer_2):
        self.attend_layer_2 = attend_layer_2
        self.attend_layer_1 = attend_layer_1
        self.__name__ = 'alpha_fn'

    def __call__(self, x):
        weights_1 = keras.layers.Dropout(0.2)(self.attend_layer_1(x[0]))
        weights_1 = BatchNormalization()(keras.layers.Dropout(0.2)(self.attend_layer_2(weights_1)))
        weights_2 = keras.layers.Dropout(0.2)(self.attend_layer_1(x[1]))
        weights_2 = BatchNormalization()(keras.layers.Dropout(0.2)(self.attend_layer_2(weights_2)))
        # batch_size x lA x lB
        attend_weights = K.batch_dot(K.permute_dimensions(weights_1, (0, 2, 1)), weights_2, axes=(1, 2))
        alpha = K.softmax(attend_weights, axis=1)
        # batch_size x lB x 1024
        alpha = K.batch_dot(K.permute_dimensions(alpha, (0, 2, 1)), x[0], axes=(2, 1))
        return alpha


class BetaFN:
    def __init__(self, attend_layer_1, attend_layer_2):
        self.attend_layer_2 = attend_layer_2
        self.attend_layer_1 = attend_layer_1
        self.__name__ = 'beta_fn'

    def __call__(self, x):
        weights_1 = keras.layers.Dropout(0.2)(self.attend_layer_1(x[0]))
        weights_1 = BatchNormalization()(keras.layers.Dropout(0.2)(self.attend_layer_2(weights_1)))
        weights_2 = keras.layers.Dropout(0.2)(self.attend_layer_1(x[1]))
        weights_2 = BatchNormalization()(keras.layers.Dropout(0.2)(self.attend_layer_2(weights_2)))
        # batch_size x lA x lB
        attend_weights = K.batch_dot(K.permute_dimensions(weights_1, (0, 2, 1)), weights_2, axes=(1, 2))
        beta = K.softmax(attend_weights, axis=2)
        # batch_size x lA x 1024
        beta = K.batch_dot(beta, x[1], axes=(2, 1))
        return beta


def model(sess, config):
    if config.debug:
        print('Importing Elmo module...')
    if config.hub.is_set("cache_dir"):
        os.environ['TFHUB_CACHE_DIR'] = config.hub.cache_dir

    elmo_model = hub.Module("https://tfhub.dev/google/elmo/1", trainable=True)

    if config.debug:
        print('Imported.')
    sess.run(tf.global_variables_initializer())
    sess.run(tf.tables_initializer())

    elmo_embeddings = ElmoEmbedding(elmo_model)

    # Attention
    attn_layer_1 = keras.layers.Dense(1024, activation='relu')
    attn_layer_2 = keras.layers.Dense(500, activation='relu')

    # Attend
    attend_layer_1 = keras.layers.Dense(1024, activation='relu')
    attend_layer_2 = keras.layers.Dense(500, activation='relu')

    # Compare
    compare_layer_1 = keras.layers.Dense(1024, activation="relu")
    compare_layer_2 = keras.layers.Dense(500, activation="relu")

    # Predict
    predict_layer_1 = keras.layers.Dense(1, activation="softmax")

    attn_fn = IntraAttn(attn_layer_1, attn_layer_2)
    alpha_fn = AlphaFN(attend_layer_1, attend_layer_2)
    beta_fn = BetaFN(attend_layer_1, attend_layer_2)

    def reduce_sum_fn(x):
        return K.sum(x, axis=1)

    # Graph
    sentence_1 = keras.layers.Input(shape=(1,), dtype="string")  # Sentences comes in as a string
    sentence_2 = keras.layers.Input(shape=(1,), dtype="string")
    embedding = keras.layers.Lambda(elmo_embeddings, output_shape=(None, 1024,))
    # batch_size x lA x 1024
    sentence_1_embedded = embedding(sentence_1)
    # batch_size x lB x 1024
    sentence_2_embedded = embedding(sentence_2)
    attn_layer = keras.layers.Lambda(attn_fn, output_shape=(None, 1024,))
    alpha_layer = keras.layers.Lambda(alpha_fn, output_shape=(None, 2048,))
    beta_layer = keras.layers.Lambda(beta_fn, output_shape=(None, 2048,))
    reduce_sum_layer = keras.layers.Lambda(reduce_sum_fn, output_shape=(500,))

    sentence_1_attn = keras.layers.concatenate([sentence_1_embedded, attn_layer(sentence_1_embedded)])
    sentence_2_attn = keras.layers.concatenate([sentence_2_embedded, attn_layer(sentence_2_embedded)])

    alpha, beta = alpha_layer([sentence_1_attn, sentence_2_attn]), beta_layer(
            [sentence_1_attn, sentence_2_attn])

    in_v1 = keras.layers.concatenate([sentence_1_attn, beta])
    in_v1 = BatchNormalization()(keras.layers.Dropout(0.2)(compare_layer_1(in_v1)))
    in_v1 = BatchNormalization()(keras.layers.Dropout(0.2)(compare_layer_2(in_v1)))
    in_v2 = keras.layers.concatenate([sentence_2_attn, alpha])
    in_v2 = BatchNormalization()(keras.layers.Dropout(0.2)(compare_layer_1(in_v2)))
    in_v2 = BatchNormalization()(keras.layers.Dropout(0.2)(compare_layer_2(in_v2)))
    v1 = reduce_sum_layer(in_v1)
    v2 = reduce_sum_layer(in_v2)

    v12 = keras.layers.concatenate([v1, v2])
    output = predict_layer_1(v12)

    # Model
    entailment_model = keras.models.Model(inputs=[sentence_1, sentence_2], outputs=output)
    entailment_model.compile(optimizer="adam", loss="binary_crossentropy", metrics=['accuracy'])
    return entailment_model


def main(config):
    train_set = SNLIDataloader('data/snli_1.0/snli_1.0_train.jsonl')
    train_set.set_preprocess_fn(preprocess_fn)
    train_set.set_output_fn(output_fn)
    dev_set = Dataloader(config, 'data/test_stories.csv', testing_data=True)
    # dev_set.set_preprocess_fn(preprocess_fn)
    dev_set.load_dataset('data/test.bin')
    dev_set.load_vocab('./data/default.voc', config.vocab_size)
    dev_set.set_output_fn(output_fn_test)
    # test_set = SNLIDataloader('data/snli_1.0/snli_1.0_test.jsonl')

    generator_training = train_set.get_batch(config.batch_size, config.n_epochs)
    generator_dev = dev_set.get_batch(config.batch_size, config.n_epochs)

    # Initialize tensorflow session
    sess = tf.Session()
    K.set_session(sess)  # Set to keras backend

    keras_model = model(sess, config)
    print(keras_model.summary())

    verbose = 0 if not config.debug else 1
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Callbacks
    tensorboard = keras.callbacks.TensorBoard(log_dir='./logs/' + timestamp + '-entailmentv1/', histogram_freq=0,
                                              batch_size=config.batch_size,
                                              write_graph=False,
                                              write_grads=True)

    model_path = os.path.abspath(
            os.path.join(os.curdir, './builds/' + timestamp))
    model_path += '-entailmentv1_checkpoint_epoch-{epoch:02d}.hdf5'

    saver = keras.callbacks.ModelCheckpoint(model_path,
                                            monitor='val_loss', verbose=verbose, save_best_only=True)

    keras_model.fit_generator(generator_training, steps_per_epoch=300,
                              epochs=config.n_epochs,
                              verbose=verbose,
                              validation_data=generator_dev,
                              validation_steps=len(dev_set) / config.batch_size,
                              callbacks=[tensorboard, saver])
