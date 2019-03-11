import numpy as np
import os
import csv
import pickle
import sys
import shutil
import gc
from copy import deepcopy

import keras
from keras import backend as K
from keras.utils import to_categorical
from keras.optimizers import Adam, SGD
from keras.callbacks import EarlyStopping, LearningRateScheduler

import tensorflow as tf

from .src.child_network_micro_search import NetworkOperation
from .src.child_network_micro_search import NetworkOperationController
from .src.child_network_micro_search import CellGenerator
from .src.child_network_micro_search import ChildNetworkController

from .src.controller_network import ControllerRNNController
from .src.utils import sgdr_learning_rate

nt = sgdr_learning_rate(n_Max=0.05, n_min=0.001, ranges=5, init_cycle=10)


class EfficientNeuralArchitectureSearch(object):
    def __init__(self,
                 x_train,
                 y_train,
                 x_test,
                 y_test,
                 child_network_name,
                 child_classes,
                 child_input_shape,
                 num_nodes=6,
                 num_opers=5,
                 controller_lstm_cell_units=32,
                 controller_baseline_decay=0.99,
                 controller_opt=Adam(lr=0.00035, decay=1e-3, amsgrad=True),
                 controller_batch_size=1,
                 controller_epochs=50,
                 controller_callbacks=[
                     EarlyStopping(
                         monitor='val_loss',
                         patience=1,
                         verbose=1,
                         mode='auto')
                 ],
                 controller_temperature=5.0,
                 controller_tanh_constant=2.5,
                 controller_normal_model_file="normal_controller.hdf5",
                 controller_reduction_model_file="reduction_controller.hdf5",
                 child_init_filters=64,
                 child_network_definition=["N", "N", "R"],
                 child_weight_directory="child_weights",
                 child_opt_loss='categorical_crossentropy',
                 child_opt=SGD(lr=0.05, decay=1e-6, nesterov=True),
                 child_opt_metrics=['accuracy'],
                 child_val_batch_size=128,
                 child_batch_size=128,
                 child_epochs=len(nt),
                 child_lr_scedule=nt,
                 start_from_record=True,
                 run_on_jupyter=True,
                 initialize_child_weight_directory=True,
                 save_to_disk=False,
                 set_from_dict=True,
                 data_gen=None,
                 data_flow_gen=None,
                 working_directory='.'):
        self.x_train = x_train
        self.y_train = y_train
        self.x_test = x_test
        self.y_test = y_test
        self.num_nodes = num_nodes
        self.num_opers = num_opers

        self.controller_lstm_cell_units = controller_lstm_cell_units
        self.controller_baseline_decay = controller_baseline_decay
        self.controller_opt = controller_opt
        self.controller_batch_size = controller_batch_size
        self.controller_epochs = controller_epochs
        self.controller_callbacks = controller_callbacks
        self.controller_temperature = controller_temperature
        self.controller_tanh_constant = controller_tanh_constant
        self.controller_input_x = np.array(
            [[[self.num_opers + self.num_nodes]]])
        self.controller_normal_model_file = os.path.join(working_directory, controller_normal_model_file)
        self.controller_reduction_model_file = os.path.join(working_directory, controller_reduction_model_file)

        self.child_network_name = child_network_name
        self.child_classes = child_classes
        self.child_input_shape = child_input_shape
        self.child_init_filters = child_init_filters
        self.child_network_definition = child_network_definition
        self.child_weight_directory = os.path.join(working_directory, child_weight_directory)
        self.child_opt_loss = child_opt_loss
        self.child_opt = child_opt
        self.child_opt_metrics = child_opt_metrics
        self.child_batch_size = child_batch_size
        self.child_epochs = child_epochs
        self.child_lr_scedule = child_lr_scedule

        self.child_train_records = []
        self.child_val_batch_size = child_val_batch_size
        self.child_train_index = self.get_child_index(self.y_train)
        self.child_val_index = self.get_child_index(self.y_test)

        self.start_from_record = start_from_record
        self.run_on_jupyter = run_on_jupyter
        self.save_to_disk = save_to_disk
        self.set_from_dict = set_from_dict
        self.data_gen = data_gen
        self.data_flow_gen = data_flow_gen
        self.initialize_child_weight_directory = initialize_child_weight_directory
        self.working_directory = working_directory

        self.reward = 0

        self.NCRC = self.define_controller_rnn(
            controller_network_name="normalcontroller",
            model_file=self.controller_normal_model_file)
        self.RCRC = self.define_controller_rnn(
            controller_network_name="reductioncontroller",
            model_file=self.controller_reduction_model_file)

        self.weight_dict = {}
        self.best_epoch_num = 0
        self.best_val_acc = 0
        self.best_normal_cell = None
        self.best_reduction_cell = None

        self._sep = "-" * 10

        self._initialize_child_weight_directory()

    def _initialize_child_weight_directory(self):
        if self.initialize_child_weight_directory:
            print("initialize: {0}".format(self.child_weight_directory))
            if os.path.exists(self.child_weight_directory):
                shutil.rmtree(self.child_weight_directory)

    def get_child_index(self, y):
        return [i for i in range(len(y))]

    def define_controller_rnn(self, controller_network_name, model_file=None):
        return ControllerRNNController(
            controller_network_name=controller_network_name,
            num_nodes=self.num_nodes,
            num_opers=self.num_opers,
            input_x=self.controller_input_x,
            reward=self.reward,
            temperature=self.controller_temperature,
            tanh_constant=self.controller_tanh_constant,
            model_file=model_file,
            lstm_cell_units=self.controller_lstm_cell_units,
            baseline_decay=self.controller_baseline_decay,
            opt=self.controller_opt)

    def train_controller_rnn(self, normal_pred_dict, reduction_pred_dict):
        self.NCRC.reward = self.reward
        self.RCRC.reward = self.reward
        print("{0} training {1} {0}".format(self._sep,
                                            self.NCRC.controller_network_name))
        self.NCRC.train_controller_rnn(
            targets=normal_pred_dict,
            batch_size=self.controller_batch_size,
            epochs=self.controller_epochs,
            callbacks=self.controller_callbacks)
        print("{0} training {1} {0}".format(self._sep,
                                            self.RCRC.controller_network_name))
        self.RCRC.train_controller_rnn(
            targets=reduction_pred_dict,
            batch_size=self.controller_batch_size,
            epochs=self.controller_epochs,
            callbacks=self.controller_callbacks)

    def define_network_operations(self):
        return NetworkOperationController(
            network_name=self.child_network_name,
            classes=self.child_classes,
            input_shape=self.child_input_shape,
            init_filters=self.child_init_filters,
            NetworkOperationInstance=NetworkOperation())

    def generate_child_cell(self, normal_cell, reduction_cell, NOC):
        return CellGenerator(
            num_nodes=self.num_nodes,
            normal_cell=normal_cell,
            reduction_cell=reduction_cell,
            NetworkOperationControllerInstance=NOC)

    def define_chile_network(self, CG, opt):
        return ChildNetworkController(
            child_network_definition=self.child_network_definition,
            CellGeneratorInstance=CG,
            weight_dict=self.weight_dict,
            weight_directory=self.child_weight_directory,
            opt_loss=self.child_opt_loss,
            opt=opt,
            opt_metrics=self.child_opt_metrics)

    def predict_architecture(self, CRC):
        controller_pred = CRC.softmax_predict()
        pred_dict = CRC.convert_pred_to_ydict(controller_pred)
        return controller_pred, pred_dict

    def get_sample_cell(self, normal_controller_pred,
                        reduction_controller_pred):
        sample_cell = {}
        random_normal_pred = self.NCRC.random_sample_softmax(
            normal_controller_pred)
        random_reduction_pred = self.RCRC.random_sample_softmax(
            reduction_controller_pred)

        sample_cell["normal_cell"] = self.NCRC.convert_pred_to_cell(
            random_normal_pred)
        sample_cell["reduction_cell"] = self.RCRC.convert_pred_to_cell(
            random_reduction_pred)
        return sample_cell

    def final_output(self, CNC, val_acc):
        if self.run_on_jupyter:
            from IPython.display import clear_output
            clear_output(wait=True)
        print("{0} FINISHED NEURAL ARCHITECTURE SEARCH {0}".format(self._sep))
        print("training records:\n{0}".format(self.child_train_records))
        print("final child network:\n")
        print(CNC.model.summary())
        print("evaluation loss: {0}\nevaluation acc: {1}".format(
            val_acc[0], val_acc[1]))

    def get_batch(self, index, size, train=True):
        _batch = np.random.choice(index, size, replace=False)
        if train:
            return self.x_train[_batch], self.y_train[_batch]
        else:
            return self.x_test[_batch], self.y_test[_batch]

    def write_record(self, epoch, lr, reward, val_loss):
        record_file = "{0}_record.csv".format(os.path.join(self.working_directory, self.child_network_name))
        with open(record_file, "a") as f:
            writer = csv.writer(f, lineterminator='\n')
            if not os.path.exists(record_file):
                writer.writerow(
                    ["epoch", "lr", "reward", "val_loss", "best_val_acc"])
            writer.writerow([epoch, lr, reward, val_loss, self.best_val_acc])
        print("saved records so far")

    def read_record(self):
        record_file = "{0}_record.csv".format(os.path.join(self.working_directory, self.child_network_name))
        rec = []
        if os.path.exists(record_file):
            with open(record_file, 'r') as f:
                reader = csv.reader(f)
                for row in reader:
                    rec.append(row)
            print("loaded records")
            return rec
        else:
            return None

    def save_best_cell(self):
        normal_cell_file = "{0}_normal_cell.pkl".format(
            os.path.join(self.working_directory, self.child_network_name))
        with open(normal_cell_file, "wb") as f:
            pickle.dump(self.best_normal_cell, f)
        reduction_cell_file = "{0}_reduction_cell.pkl".format(
            os.path.join(self.working_directory, self.child_network_name))
        with open(reduction_cell_file, "wb") as f:
            pickle.dump(self.best_reduction_cell, f)
        print("saved best cells")

    def load_best_cell(self):
        normal_cell_file = "{0}_normal_cell.pkl".format(
            os.path.join(self.working_directory, self.child_network_name))
        with open(normal_cell_file, "rb") as f:
            self.best_normal_cell = pickle.load(f)
        reduction_cell_file = "{0}_reduction_cell.pkl".format(
            os.path.join(self.working_directory, self.child_network_name))
        with open(reduction_cell_file, "rb") as f:
            self.best_reduction_cell = pickle.load(f)
        print("loaded best cells")

    def search_neural_architecture(self):
        if self.start_from_record:
            rec = self.read_record()
            if rec is not None:
                starting_epoch = int(rec[-1][0]) + 1
                self.best_val_acc = float(rec[-1][4])
                self.load_best_cell()
            else:
                starting_epoch = 0
        for e in range(starting_epoch, self.child_epochs):
            print("SEARCH EPOCH: {0} / {1}".format(e, self.child_epochs))
            normal_controller_pred, normal_pred_dict = self.predict_architecture(
                self.NCRC)
            reduction_controller_pred, reduction_pred_dict = self.predict_architecture(
                self.RCRC)

            sample_cell = self.get_sample_cell(normal_controller_pred,
                                               reduction_controller_pred)

            x_val_batch, y_val_batch = self.get_batch(
                self.child_val_index, self.child_val_batch_size, False)
            for k, v in sample_cell.items():
                print("{0}: {1}".format(k, v))

            self.child_opt = SGD(lr=self.child_lr_scedule[e], nesterov=True)

            CG = self.generate_child_cell(sample_cell["normal_cell"],
                                          sample_cell["reduction_cell"],
                                          self.define_network_operations())
            CNC = self.define_chile_network(CG, self.child_opt)
            CNC.set_weight_to_layer(set_from_dict=self.set_from_dict)
            CNC.train_child_network(
                x_train=self.x_train,
                y_train=self.y_train,
                validation_data=(x_val_batch, y_val_batch),
                batch_size=self.child_batch_size,
                epochs=5,
                data_gen=self.data_gen,
                data_flow_gen=self.data_flow_gen)
            CNC.fetch_layer_weight(save_to_disk=self.save_to_disk)
            for k, v in CNC.weight_dict.items():
                self.weight_dict[k] = v
            val_acc = CNC.evaluate_child_network(x_val_batch, y_val_batch)
            print(val_acc)
            self.reward = val_acc[1]

            if self.best_val_acc < val_acc[1]:
                self.best_epoch_num = e
                self.best_val_acc = val_acc[1]
                self.best_normal_cell = sample_cell["normal_cell"]
                self.best_reduction_cell = sample_cell["reduction_cell"]

            self.write_record(e, self.child_lr_scedule[e], self.reward,
                              val_acc[0])
            self.save_best_cell()

            child_train_record = {}
            child_train_record["normal_cell"] = sample_cell["normal_cell"]
            child_train_record["reduction_cell"] = sample_cell[
                "reduction_cell"]
            child_train_record["val_loss"] = val_acc[0]
            child_train_record["reward"] = val_acc[1]
            print("epoch: {0}\nrecord: ".format(e))
            for k, v in child_train_record.items():
                print("{0}: {1}".format(k, v))
            self.child_train_records.append(child_train_record)

            if e == self.child_epochs - 1:
                self.final_output(CNC, val_acc)
                break

            CNC.close_tf_session()
            del CNC.weight_dict
            del CNC.model
            del CNC.CG
            del CNC
            del CG.NOC
            del CG
            del x_val_batch
            del y_val_batch
            gc.collect()

            print("{0} train controller rnn {0}".format(self._sep))
            self.train_controller_rnn(normal_pred_dict, reduction_pred_dict)
            self.NCRC.save_model()
            self.RCRC.save_model()

            print("{0} training finished {0}".format(self._sep))
            print("{0} FINISHED SEARCH EPOCH {1} / {2} {0}".format(
                self._sep, e, self.child_epochs))
            if self.run_on_jupyter:
                from IPython.display import clear_output
                clear_output(wait=True)

    def train_best_cells(self,
                         normal_cell=None,
                         reduction_cell=None,
                         child_callbacks=[
                             EarlyStopping(
                                 monitor='val_loss',
                                 patience=20,
                                 verbose=1,
                                 mode='auto')
                         ],
                         child_opt=Adam(lr=0.001, decay=1e-6, amsgrad=True),
                         child_epochs=100):
        if normal_cell is None:
            normal_cell = self.best_normal_cell
        if reduction_cell is None:
            reduction_cell = self.best_reduction_cell

        print("BEST VAL ACCURACY WHILE SEARCH: {0}".format(self.best_val_acc))
        print("BEST NORMAL CELL: \n{0}".format(self.best_normal_cell))
        print("BEST REDUCTION CELL: \n{0}".format(self.best_reduction_cell))

        CG = self.generate_child_cell(normal_cell, reduction_cell,
                                      self.define_network_operations())
        CNC = self.define_chile_network(CG, child_opt)

        print("MODEL SUMMARY:\n")
        print(CNC.model.summary())
        CNC.set_weight_to_layer(set_from_dict=self.set_from_dict)

        CNC.train_child_network(
            x_train=self.x_train,
            y_train=self.y_train,
            validation_data=(self.x_test, self.y_test),
            batch_size=self.child_batch_size,
            epochs=child_epochs,
            callbacks=child_callbacks,
            data_gen=self.data_gen,
            data_flow_gen=self.data_flow_gen)
        CNC.fetch_layer_weight(save_to_disk=self.save_to_disk)

        print("{0} TRAINING FINISHED {0}".format(self._sep))

        val_acc = CNC.evaluate_child_network(self.x_test, self.y_test)

        print("EVALUATION LOSS: {0}\nEVALUATION ACCURACY: {1}".format(
            val_acc[0], val_acc[1]))
        self.best_val_acc = val_acc[1]
