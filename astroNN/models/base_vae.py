import json
import os
import time
from abc import ABC

import numpy as np
from tqdm import tqdm
from tensorflow import keras as tfk
from astroNN.config import MULTIPROCESS_FLAG
from astroNN.config import _astroNN_MODEL_NAME
from astroNN.datasets import H5Loader
from astroNN.models.base_master_nn import NeuralNetMaster
from astroNN.nn.callbacks import VirutalCSVLogger
from astroNN.nn.losses import (
    mean_squared_error,
    mean_error,
    mean_absolute_error,
    mean_squared_reconstruction_error,
)
from astroNN.nn.utilities import Normalizer
from astroNN.nn.utilities.generator import GeneratorMaster
from astroNN.shared.dict_tools import dict_np_to_dict_list, list_to_dict
from astroNN.shared.warnings import deprecated, deprecated_copy_signature
from sklearn.model_selection import train_test_split
import tensorflow as tf
from tensorflow.python.keras.engine import data_adapter
from tensorflow.python.util import nest

regularizers = tfk.regularizers
ReduceLROnPlateau = tfk.callbacks.ReduceLROnPlateau
Adam = tfk.optimizers.Adam


class CVAEDataGenerator(GeneratorMaster):
    """
    To generate data to NN

    :param batch_size: batch size
    :type batch_size: int
    :param shuffle: Whether to shuffle batches or not
    :type shuffle: bool
    :param data: List of data to NN
    :type data: list
    :param manual_reset: Whether need to reset the generator manually, usually it is handled by tensorflow
    :type manual_reset: bool
    :param sample_weight: Sample weights (if any)
    :type sample_weight: Union([NoneType, ndarray])
    :History:
        | 2017-Dec-02 - Written - Henry Leung (University of Toronto)
        | 2019-Feb-17 - Updated - Henry Leung (University of Toronto)
    """

    def __init__(
        self,
        batch_size,
        shuffle,
        steps_per_epoch,
        data,
        manual_reset=False,
        sample_weight=None,
    ):
        super().__init__(
            batch_size=batch_size,
            shuffle=shuffle,
            steps_per_epoch=steps_per_epoch,
            data=data,
            manual_reset=manual_reset,
        )
        self.inputs = self.data[0]
        self.recon_inputs = self.data[1]
        self.sample_weight = sample_weight

        # initial idx
        self.idx_list = self._get_exploration_order(
            range(self.inputs["input"].shape[0])
        )

    def _data_generation(self, idx_list_temp):
        x = self.input_d_checking(self.inputs, idx_list_temp)
        y = self.input_d_checking(self.recon_inputs, idx_list_temp)
        if self.sample_weight is not None:
            return x, y, self.sample_weight[idx_list_temp]
        else:
            return x, y

    def __getitem__(self, index):
        return self._data_generation(
            self.idx_list[index * self.batch_size : (index + 1) * self.batch_size]
        )

    def on_epoch_end(self):
        # shuffle the list when epoch ends for the next epoch
        self.idx_list = self._get_exploration_order(
            range(self.inputs["input"].shape[0])
        )


class CVAEPredDataGenerator(GeneratorMaster):
    """
    To generate data to NN for prediction

    :param batch_size: batch size
    :type batch_size: int
    :param shuffle: Whether to shuffle batches or not
    :type shuffle: bool
    :param data: List of data to NN
    :type data: list
    :param manual_reset: Whether need to reset the generator manually, usually it is handled by tensorflow
    :type manual_reset: bool
    :param pbar: tqdm progress bar
    :type pbar: obj
    :History:
        | 2017-Dec-02 - Written - Henry Leung (University of Toronto)
        | 2019-Feb-17 - Updated - Henry Leung (University of Toronto)
    """

    def __init__(
        self, batch_size, shuffle, steps_per_epoch, data, manual_reset=True, pbar=None
    ):
        super().__init__(
            batch_size=batch_size,
            shuffle=shuffle,
            steps_per_epoch=steps_per_epoch,
            data=data,
            manual_reset=manual_reset,
        )
        self.inputs = self.data[0]
        self.pbar = pbar

        # initial idx
        self.idx_list = self._get_exploration_order(
            range(self.inputs["input"].shape[0])
        )
        self.current_idx = -1

    def _data_generation(self, idx_list_temp):
        # Generate data
        x = self.input_d_checking(self.inputs, idx_list_temp)
        return x

    def __getitem__(self, index):
        x = self._data_generation(
            self.idx_list[index * self.batch_size : (index + 1) * self.batch_size]
        )
        if self.pbar and index > self.current_idx:
            self.pbar.update(self.batch_size)
        self.current_idx = index
        return x

    def on_epoch_end(self):
        # shuffle the list when epoch ends for the next epoch
        self.idx_list = self._get_exploration_order(
            range(self.inputs["input"].shape[0])
        )


class ConvVAEBase(NeuralNetMaster, ABC):
    """
    Top-level class for a Convolutional Variational Autoencoder

    :History: 2018-Jan-06 - Written - Henry Leung (University of Toronto)
    """

    def __init__(self):
        super().__init__()
        self.name = "Convolutional Variational Autoencoder"
        self._model_type = "CVAE"
        self.initializer = None
        self.activation = None
        self._last_layer_activation = None
        self.num_filters = None
        self.filter_len = None
        self.pool_length = None
        self.num_hidden = None
        self.reduce_lr_epsilon = None
        self.reduce_lr_min = None
        self.reduce_lr_patience = None
        self.l1 = None
        self.l2 = None
        self.maxnorm = None
        self.latent_dim = None
        self.val_size = 0.1
        self.dropout_rate = 0.0

        self.keras_vae = None
        self.keras_encoder = None
        self.keras_decoder = None
        self.loss = None

        self._input_shape = None

        self.input_norm_mode = 255
        self.labels_norm_mode = 255
        self.input_mean = None
        self.input_std = None
        self.labels_mean = None
        self.labels_std = None

    def compile(
        self,
        optimizer=None,
        loss=None,
        metrics=None,
        weighted_metrics=None,
        loss_weights=None,
        sample_weight_mode=None,
    ):
        self.keras_encoder, self.keras_decoder = self.model()
        self.keras_model = tfk.Model(inputs=[self.keras_encoder.inputs], outputs=[self.keras_decoder(self.keras_encoder.outputs[2])])
        
        if optimizer is not None:
            self.optimizer = optimizer
        elif self.optimizer is None or self.optimizer == "adam":
            self.optimizer = Adam(
                learning_rate=self.lr,
                beta_1=self.beta_1,
                beta_2=self.beta_2,
                epsilon=self.optimizer_epsilon,
                decay=0.0,
            )
        if metrics is not None:
            self.metrics = metrics
        self.loss = (
            mean_squared_reconstruction_error if not (loss and self.loss) else loss
        )
        # self.metrics = [mean_absolute_error, mean_error] if not self.metrics else self.metrics
        self.metrics = []

        self.keras_model.compile(
            loss=self.loss,
            optimizer=self.optimizer,
            metrics=self.metrics,
            weighted_metrics=weighted_metrics,
            loss_weights=loss_weights,
            sample_weight_mode=sample_weight_mode,
        )
        self.keras_model.total_loss_tracker = tfk.metrics.Mean(name="loss")
        self.keras_model.reconstruction_loss_tracker = tfk.metrics.Mean(
            name="reconstruction_loss"
        )
        self.keras_model.kl_loss_tracker = tfk.metrics.Mean(name="kl_loss")

        # inject custom training step if needed
        try:
            self.custom_train_step()
        except NotImplementedError:
            pass
        except TypeError:
            self.keras_model.train_step = self.custom_train_step
        # inject custom testing  step if needed
        try:
            self.custom_test_step()
        except NotImplementedError:
            pass
        except TypeError:
            self.keras_model.test_step = self.custom_test_step

        return None

    def recompile(
        self,
        loss=None,
        weighted_metrics=None,
        loss_weights=None,
        sample_weight_mode=None,
    ):
        """
        To be used when you need to recompile a already existing model
        """
        self.keras_model.compile(
            loss=self.loss,
            optimizer=self.optimizer,
            metrics=self.metrics,
            weighted_metrics=weighted_metrics,
            loss_weights=loss_weights,
            sample_weight_mode=sample_weight_mode,
        )

    def custom_train_step(self, data):
        """
        Custom training logic

        :param data:
        :return:
        """
        data = data_adapter.expand_1d(data)
        x, y, sample_weight = data_adapter.unpack_x_y_sample_weight(data)
        # TODO: properly fix this
        y = y["output"]

        # Run forward pass.
        with tf.GradientTape() as tape:
            z_mean, z_log_var, z = self.keras_encoder(x, training=True)
            y_pred = self.keras_decoder(z, training=True)
            reconstruction_loss = self.loss(y, y_pred, sample_weight=sample_weight)
            kl_loss = -0.5 * (1 + z_log_var - tf.square(z_mean) - tf.exp(z_log_var))
            kl_loss = tf.reduce_mean(tf.reduce_sum(kl_loss, axis=1))
            total_loss = reconstruction_loss + kl_loss
            
        # Run backwards pass.
        grads = tape.gradient(total_loss, self.keras_model.trainable_weights)
        self.keras_model.optimizer.apply_gradients(zip(grads, self.keras_model.trainable_weights))
        # self.keras_model.compiled_metrics.update_state(y, y_pred, sample_weight)

        self.keras_model.total_loss_tracker.update_state(total_loss)
        self.keras_model.reconstruction_loss_tracker.update_state(reconstruction_loss)
        self.keras_model.kl_loss_tracker.update_state(kl_loss)
        return_metrics = {
            "loss": self.keras_model.total_loss_tracker.result(),
            "reconstruction_loss": self.keras_model.reconstruction_loss_tracker.result(),
            "kl_loss": self.keras_model.kl_loss_tracker.result(),
        }
        # Collect metrics to return
        for metric in self.keras_model.metrics:
            result = metric.result()
            if isinstance(result, dict):
                return_metrics.update(result)
            else:
                return_metrics[metric.name] = result
        return return_metrics

    def custom_test_step(self, data):
        data = data_adapter.expand_1d(data)
        x, y, sample_weight = data_adapter.unpack_x_y_sample_weight(data)
        y = y["output"]

        z_mean, z_log_var, z = self.keras_encoder(x, training=False)
        y_pred = self.keras_decoder(z, training=False)
        reconstruction_loss = self.loss(y, y_pred, sample_weight=sample_weight)
        kl_loss = -0.5 * (1 + z_log_var - tf.square(z_mean) - tf.exp(z_log_var))
        kl_loss = tf.reduce_mean(tf.reduce_sum(kl_loss, axis=1))
        total_loss = reconstruction_loss + kl_loss
        
        self.keras_model.total_loss_tracker.update_state(total_loss)
        self.keras_model.reconstruction_loss_tracker.update_state(reconstruction_loss)
        self.keras_model.kl_loss_tracker.update_state(kl_loss)
        return_metrics = {
            "loss": self.keras_model.total_loss_tracker.result(),
            "reconstruction_loss": self.keras_model.reconstruction_loss_tracker.result(),
            "kl_loss": self.keras_model.kl_loss_tracker.result(),
        }
        
        for metric in self.keras_model.metrics:
            result = metric.result()
            if isinstance(result, dict):
                return_metrics.update(result)
            else:
                return_metrics[metric.name] = result
        return return_metrics

    def pre_training_checklist_child(
        self, input_data, input_recon_target, sample_weight
    ):        
        if self.task == "classification":
            raise RuntimeError("astroNN VAE does not support classification task")
        elif self.task == "binary_classification":
            raise RuntimeError(
                "astroNN VAE does not support binary classification task"
            )

        input_data, input_recon_target = self.pre_training_checklist_master(
            input_data, input_recon_target
        )

        if isinstance(input_data, H5Loader):
            self.targetname = input_data.target
            input_data, input_recon_target = input_data.load()
        # check if exists (existing means the model has already been trained (e.g. fine-tuning), so we do not need calculate mean/std again)
        if self.input_normalizer is None:
            self.input_normalizer = Normalizer(
                mode=self.input_norm_mode, verbose=self.verbose
            )
            self.labels_normalizer = Normalizer(
                mode=self.labels_norm_mode, verbose=self.verbose
            )

            norm_data = self.input_normalizer.normalize(input_data)
            self.input_mean, self.input_std = (
                self.input_normalizer.mean_labels,
                self.input_normalizer.std_labels,
            )
            norm_labels = self.labels_normalizer.normalize(input_recon_target)
            self.labels_mean, self.labels_std = (
                self.labels_normalizer.mean_labels,
                self.labels_normalizer.std_labels,
            )
        else:
            norm_data = self.input_normalizer.normalize(input_data, calc=False)
            norm_labels = self.labels_normalizer.normalize(
                input_recon_target, calc=False
            )

        if (
            self.keras_model is None
        ):  # only compile if there is no keras_model, e.g. fine-tuning does not required
            self.compile()

        norm_data = self._tensor_dict_sanitize(norm_data, self.keras_model.input_names)
        norm_labels = self._tensor_dict_sanitize(
            norm_labels, self.keras_model.output_names
        )
        
        if self.has_val:
            self.train_idx, self.val_idx = train_test_split(
                np.arange(self.num_train + self.val_num), test_size=self.val_size
            )
        else:
            self.train_idx = np.arange(self.num_train + self.val_num)
            # just dummy, to minimize modification needed
            self.val_idx = np.arange(self.num_train + self.val_num)[:2]

        norm_data_training = {}
        norm_data_val = {}
        norm_labels_training = {}
        norm_labels_val = {}
        for name in norm_data.keys():
            norm_data_training.update({name: norm_data[name][self.train_idx]})
            norm_data_val.update({name: norm_data[name][self.val_idx]})
        for name in norm_labels.keys():
            norm_labels_training.update({name: norm_labels[name][self.train_idx]})
            norm_labels_val.update({name: norm_labels[name][self.val_idx]})

        if sample_weight is not None:
            sample_weight_training = sample_weight[self.train_idx]
            sample_weight_val = sample_weight[self.val_idx]
        else:
            sample_weight_training = None
            sample_weight_val = None

        self.training_generator = CVAEDataGenerator(
            batch_size=self.batch_size,
            shuffle=True,
            steps_per_epoch=self.num_train // self.batch_size,
            data=[norm_data_training, norm_labels_training],
            manual_reset=False,
            sample_weight=sample_weight_training,
        )
        if self.has_val:
            val_batchsize = (
                self.batch_size
                if len(self.val_idx) > self.batch_size
                else len(self.val_idx)
            )
            self.validation_generator = CVAEDataGenerator(
                batch_size=val_batchsize,
                shuffle=True,
                steps_per_epoch=max(self.val_num // self.batch_size, 1),
                data=[norm_data_val, norm_labels_val],
                manual_reset=True,
                sample_weight=sample_weight_val,
            )

        return input_data, input_recon_target

    def fit(self, input_data, input_recon_target, sample_weight=None):
        """
        Train a Convolutional Autoencoder

        :param input_data: Data to be trained with neural network
        :type input_data: ndarray
        :param input_recon_target: Data to be reconstructed
        :type input_recon_target: ndarray
        :param sample_weight: Sample weights (if any)
        :type sample_weight: Union([NoneType, ndarray])
        :return: None
        :rtype: NoneType
        :History: 2017-Dec-06 - Written - Henry Leung (University of Toronto)
        """

        # Call the checklist to create astroNN folder and save parameters
        self.pre_training_checklist_child(
            input_data, input_recon_target, sample_weight
        )

        reduce_lr = ReduceLROnPlateau(
            monitor="loss",
            factor=0.5,
            min_delta=self.reduce_lr_epsilon,
            patience=self.reduce_lr_patience,
            min_lr=self.reduce_lr_min,
            mode="min",
            verbose=self.verbose,
        )

        self.virtual_cvslogger = VirutalCSVLogger()

        self.__callbacks = [
            reduce_lr,
            self.virtual_cvslogger,
        ]  # default must have unchangeable callbacks

        if self.callbacks is not None:
            if isinstance(self.callbacks, list):
                self.__callbacks.extend(self.callbacks)
            else:
                self.__callbacks.append(self.callbacks)

        start_time = time.time()

        self.keras_model.fit(
            self.training_generator,
            validation_data=self.validation_generator,
            epochs=self.max_epochs,
            verbose=self.verbose,
            workers=os.cpu_count(),
            callbacks=self.__callbacks,
            use_multiprocessing=MULTIPROCESS_FLAG,
        )

        print(f"Completed Training, {(time.time() - start_time):.{2}f}s in total")

        if self.autosave is True:
            # Call the post training checklist to save parameters
            self.save()

        return None

    def fit_on_batch(self, input_data, input_recon_target, sample_weight=None):
        """
        Train a AutoEncoder by running a single gradient update on all of your data, suitable for fine-tuning

        :param input_data: Data to be trained with neural network
        :type input_data: ndarray
        :param input_recon_target: Data to be reconstructed
        :type input_recon_target: ndarray
        :param sample_weight: Sample weights (if any)
        :type sample_weight: Union([NoneType, ndarray])
        :return: None
        :rtype: NoneType
        :History: 2018-Aug-25 - Written - Henry Leung (University of Toronto)
        """

        input_data, input_recon_target = self.pre_training_checklist_master(
            input_data, input_recon_target
        )

        # check if exists (existing means the model has already been trained (e.g. fine-tuning), so we do not need calculate mean/std again)
        if self.input_normalizer is None:
            self.input_normalizer = Normalizer(
                mode=self.input_norm_mode, verbose=self.verbose
            )
            self.labels_normalizer = Normalizer(
                mode=self.labels_norm_mode, verbose=self.verbose
            )

            norm_data = self.input_normalizer.normalize(input_data)
            self.input_mean, self.input_std = (
                self.input_normalizer.mean_labels,
                self.input_normalizer.std_labels,
            )
            norm_labels = self.labels_normalizer.normalize(input_recon_target)
            self.labels_mean, self.labels_std = (
                self.labels_normalizer.mean_labels,
                self.labels_normalizer.std_labels,
            )
        else:
            norm_data = self.input_normalizer.normalize(input_data, calc=False)
            norm_labels = self.labels_normalizer.normalize(
                input_recon_target, calc=False
            )

        norm_data = self._tensor_dict_sanitize(norm_data, self.keras_model.input_names)
        norm_labels = self._tensor_dict_sanitize(
            norm_labels, self.keras_model.output_names
        )

        start_time = time.time()

        fit_generator = CVAEDataGenerator(
            batch_size=input_data["input"].shape[0],
            shuffle=False,
            steps_per_epoch=1,
            data=[norm_data, norm_labels],
            sample_weight=sample_weight,
        )

        scores = self.keras_model.fit(
            fit_generator,
            epochs=1,
            verbose=self.verbose,
            workers=os.cpu_count(),
            use_multiprocessing=MULTIPROCESS_FLAG,
        )

        print(
            f"Completed Training on Batch, {(time.time() - start_time):.{2}f}s in total"
        )

        return None

    def post_training_checklist_child(self):
        self.keras_model.save(self.fullfilepath + _astroNN_MODEL_NAME)
        print(
            _astroNN_MODEL_NAME
            + f" saved to {(self.fullfilepath + _astroNN_MODEL_NAME)}"
        )

        self.hyper_txt.write(f"Dropout Rate: {self.dropout_rate} \n")
        self.hyper_txt.flush()
        self.hyper_txt.close()

        data = {
            "id": self.__class__.__name__,
            "pool_length": self.pool_length,
            "filterlen": self.filter_len,
            "filternum": self.num_filters,
            "hidden": self.num_hidden,
            "input": self._input_shape,
            "labels": self._labels_shape,
            "task": self.task,
            "activation": self.activation,
            "input_mean": dict_np_to_dict_list(self.input_mean),
            "labels_mean": dict_np_to_dict_list(self.labels_mean),
            "input_std": dict_np_to_dict_list(self.input_std),
            "labels_std": dict_np_to_dict_list(self.labels_std),
            "valsize": self.val_size,
            "targetname": self.targetname,
            "dropout_rate": self.dropout_rate,
            "l1": self.l1,
            "l2": self.l2,
            "maxnorm": self.maxnorm,
            "input_norm_mode": self.input_normalizer.normalization_mode,
            "labels_norm_mode": self.labels_normalizer.normalization_mode,
            "batch_size": self.batch_size,
            "latent": self.latent_dim,
        }

        with open(self.fullfilepath + "/astroNN_model_parameter.json", "w") as f:
            json.dump(data, f, indent=4, sort_keys=True)

    def predict(self, input_data):
        """
        Use the neural network to do inference and get reconstructed data

        :param input_data: Data to be inferred with neural network
        :type input_data: ndarray
        :return: reconstructed data
        :rtype: ndarry
        :History: 2017-Dec-06 - Written - Henry Leung (University of Toronto)
        """
        input_data = self.pre_testing_checklist_master(input_data)

        if self.input_normalizer is not None:
            input_array = self.input_normalizer.normalize(input_data, calc=False)
        else:
            # Prevent shallow copy issue
            input_array = np.array(input_data)
            input_array -= self.input_mean
            input_array /= self.input_std

        total_test_num = input_data["input"].shape[0]  # Number of testing data

        # for number of training data smaller than batch_size
        if total_test_num < self.batch_size:
            self.batch_size = total_test_num

        # Due to the nature of how generator works, no overlapped prediction
        data_gen_shape = (total_test_num // self.batch_size) * self.batch_size
        remainder_shape = total_test_num - data_gen_shape  # Remainder from generator

        predictions = np.zeros((total_test_num, self._labels_shape["output"], 1))

        norm_data_main = {}
        norm_data_remainder = {}
        for name in input_array.keys():
            norm_data_main.update({name: input_array[name][:data_gen_shape]})
            norm_data_remainder.update({name: input_array[name][data_gen_shape:]})

        norm_data_main = self._tensor_dict_sanitize(
            norm_data_main, self.keras_model.input_names
        )
        norm_data_remainder = self._tensor_dict_sanitize(
            norm_data_remainder, self.keras_model.input_names
        )

        start_time = time.time()
        print("Starting Inference")

        # Data Generator for prediction
        with tqdm(total=total_test_num, unit="sample") as pbar:
            pbar.set_description_str("Prediction progress: ")
            prediction_generator = CVAEPredDataGenerator(
                batch_size=self.batch_size,
                shuffle=False,
                steps_per_epoch=total_test_num // self.batch_size,
                data=[norm_data_main],
            )
            result = np.asarray(self.keras_model.predict(prediction_generator, verbose=0))

            if remainder_shape != 0:
                remainder_generator = CVAEPredDataGenerator(
                    batch_size=remainder_shape,
                    shuffle=False,
                    steps_per_epoch=1,
                    data=[norm_data_remainder],
                )
                pbar.update(remainder_shape)
                remainder_result = np.asarray(
                    self.keras_model.predict(remainder_generator, verbose=0)
                )
                result = np.concatenate((result, remainder_result))
                
        predictions[:] = result

        if self.labels_normalizer is not None:
            # TODO: handle named output in the future
            predictions[:, :, 0] = self.labels_normalizer.denormalize(
                list_to_dict(self.keras_model.output_names, predictions[:, :, 0])
            )["output"]
        else:
            predictions[:, :, 0] *= self.labels_std
            predictions[:, :, 0] += self.labels_mean

        print(f"Completed Inference, {(time.time() - start_time):.{2}f}s elapsed")

        return predictions

    def predict_encoder(self, input_data):
        """
        Use the neural network to do inference and get the hidden layer encoding/representation

        :param input_data: Data to be inferred with neural network
        :type input_data: ndarray
        :return: hidden layer encoding/representation mean and std
        :rtype: ndarray
        :History: 2017-Dec-06 - Written - Henry Leung (University of Toronto)
        """
        input_data = self.pre_testing_checklist_master(input_data)
        # Prevent shallow copy issue
        if self.input_normalizer is not None:
            input_array = self.input_normalizer.normalize(input_data, calc=False)
        else:
            # Prevent shallow copy issue
            input_array = np.array(input_data)
            input_array -= self.input_mean
            input_array /= self.input_std

        total_test_num = input_data["input"].shape[0]  # Number of testing data

        # for number of training data smaller than batch_size
        if total_test_num < self.batch_size:
            self.batch_size = input_data["input"].shape[0]

        # Due to the nature of how generator works, no overlapped prediction
        data_gen_shape = (total_test_num // self.batch_size) * self.batch_size
        remainder_shape = total_test_num - data_gen_shape  # Remainder from generator

        norm_data_main = {}
        norm_data_remainder = {}
        for name in input_array.keys():
            norm_data_main.update({name: input_array[name][:data_gen_shape]})
            norm_data_remainder.update({name: input_array[name][data_gen_shape:]})

        encoding_mean = np.zeros((total_test_num, self.latent_dim))
        encoding_uncertainty = np.zeros((total_test_num, self.latent_dim))
        encoding = np.zeros((total_test_num, self.latent_dim))

        start_time = time.time()
        print("Starting Inference on Encoder")

        # Data Generator for prediction
        prediction_generator = CVAEPredDataGenerator(
            batch_size=self.batch_size,
            shuffle=False,
            steps_per_epoch=total_test_num // self.batch_size,
            data=[norm_data_main],
        )
        z_mean, z_log_var, z = np.asarray(
            self.keras_encoder.predict(prediction_generator)
        )
        
        encoding_mean[:data_gen_shape] = z_mean
        encoding_uncertainty[:data_gen_shape] = np.exp(0.5 * z_log_var)
        encoding[:data_gen_shape] = z
        
        if remainder_shape != 0:
            # assume its caused by mono images, so need to expand dim by 1
            for name in input_array.keys():
                if len(norm_data_remainder[name][0].shape) != len(
                    self._input_shape[name]
                ):
                    norm_data_remainder.update(
                        {name: np.expand_dims(norm_data_remainder[name], axis=-1)}
                    )
            z_mean, z_log_var, z = self.keras_encoder.predict(norm_data_remainder)
            encoding_mean[data_gen_shape:] = z_mean
            encoding_uncertainty[data_gen_shape:] = np.exp(0.5 * z_log_var)
            encoding[data_gen_shape:] = z
        print(
            f"Completed Inference on Encoder, {(time.time() - start_time):.{2}f}s elapsed"
        )

        return encoding_mean, encoding_uncertainty, encoding

    def evaluate(self, input_data, labels):
        """
        Evaluate neural network by provided input data and labels/reconstruction target to get back a metrics score

        :param input_data: Data to be inferred with neural network
        :type input_data: ndarray
        :param labels: labels
        :type labels: ndarray
        :return: metrics score
        :rtype: float
        :History: 2018-May-20 - Written - Henry Leung (University of Toronto)
        """
        self.has_model_check()
        input_data = {"input": input_data}
        labels = {"output": labels}
        input_data = list_to_dict(self.keras_model.input_names, input_data)
        labels = list_to_dict(self.keras_model.output_names, labels)

        # check if exists (existing means the model has already been trained (e.g. fine-tuning), so we do not need calculate mean/std again)
        if self.input_normalizer is None:
            self.input_normalizer = Normalizer(
                mode=self.input_norm_mode, verbose=self.verbose
            )
            self.labels_normalizer = Normalizer(
                mode=self.labels_norm_mode, verbose=self.verbose
            )

            norm_data = self.input_normalizer.normalize(input_data)
            self.input_mean, self.input_std = (
                self.input_normalizer.mean_labels,
                self.input_normalizer.std_labels,
            )
            norm_labels = self.labels_normalizer.normalize(labels)
            self.labels_mean, self.labels_std = (
                self.labels_normalizer.mean_labels,
                self.labels_normalizer.std_labels,
            )
        else:
            norm_data = self.input_normalizer.normalize(input_data, calc=False)
            norm_labels = self.labels_normalizer.normalize(labels, calc=False)

        norm_data = self._tensor_dict_sanitize(norm_data, self.keras_model.input_names)
        norm_labels = self._tensor_dict_sanitize(
            norm_labels, self.keras_model.output_names
        )

        total_num = input_data["input"].shape[0]
        eval_batchsize = self.batch_size if total_num > self.batch_size else total_num
        steps = total_num // self.batch_size if total_num > self.batch_size else 1

        start_time = time.time()
        print("Starting Evaluation")

        evaluate_generator = CVAEDataGenerator(
            batch_size=eval_batchsize,
            shuffle=False,
            steps_per_epoch=steps,
            data=[norm_data, norm_labels],
        )

        scores = self.keras_model.evaluate(evaluate_generator)
        if isinstance(scores, float):  # make sure scores is iterable
            scores = list(str(scores))
        outputname = self.keras_model.output_names
        funcname = self.keras_model.metrics_names

        print(f"Completed Evaluation, {(time.time() - start_time):.{2}f}s elapsed")

        return list_to_dict(funcname, scores)

    @deprecated_copy_signature(fit)
    def train(self, *args, **kwargs):
        return self.fit(*args, **kwargs)

    @deprecated_copy_signature(fit_on_batch)
    def train_on_batch(self, *args, **kwargs):
        return self.fit_on_batch(*args, **kwargs)

    @deprecated_copy_signature(predict)
    def test(self, *args, **kwargs):
        return self.predict(*args, **kwargs)

    @deprecated_copy_signature(predict_encoder)
    def test_encoder(self, *args, **kwargs):
        return self.predict_encoder(*args, **kwargs)
