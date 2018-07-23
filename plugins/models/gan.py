from comet_ml import Experiment
import keras
from keras.layers import Input, Dense, Reshape, Flatten, Dropout
from keras.layers import BatchNormalization, Activation, ZeroPadding2D
from keras.layers.advanced_activations import LeakyReLU
from keras.layers.convolutional import UpSampling2D, Conv2D, MaxPooling2D, Conv2DTranspose
from keras.models import Sequential, Model
from keras.optimizers import Adam
import os
import gc
import numpy as np
import tensorflow as tf

from deepprofiler.learning.model import DeepProfilerModel
import deepprofiler.learning.validation


#######################################################
# Based on https://github.com/eriklindernoren/Keras-GAN
#######################################################


class GAN(object):
    def __init__(self, config, crop_generator, val_crop_generator):

        if config['model']['conv_blocks'] < 1:
            raise ValueError("At least 1 convolutional block is required.")

        self.config = config
        self.crop_generator = crop_generator
        self.val_crop_generator = val_crop_generator
        self.img_rows = config["sampling"]["box_size"]
        self.img_cols = config["sampling"]["box_size"]
        self.channels = len(config["image_set"]["channels"])
        self.img_shape = (self.img_rows, self.img_cols, self.channels)
        self.latent_dim = config["model"]["latent_dim"]

        # optimizer = Adam(0.0002, 0.5)
        optimizer = Adam(config["model"]["params"]["learning_rate"], 0.5)

        # Build and compile the discriminator
        self.discriminator = self.build_discriminator()
        self.discriminator.compile(loss='binary_crossentropy',
            optimizer=optimizer,
            metrics=['accuracy'])  # TODO

        # Build the generator
        self.generator = self.build_generator()

        # The generator takes noise as input and generates imgs
        z = Input(shape=(self.latent_dim,))
        img = self.generator(z)

        # For the combined model we will only train the generator
        self.discriminator.trainable = False

        # The discriminator takes generated images as input and determines validity
        validity = self.discriminator(img)

        # The combined model  (stacked generator and discriminator)
        # Trains the generator to fool the discriminator
        self.combined = Model(z, validity)
        self.combined.compile(loss='binary_crossentropy', optimizer=optimizer)  # TODO

    def build_generator(self):

        # model = Sequential(name='generator')

        s = self.config["sampling"]["box_size"] // 2 ** self.config["model"]["conv_blocks"]
        if s < 1:
            raise ValueError("Too many convolutional blocks for the specified crop size!")
        noise = Input(shape=(self.latent_dim,))
        x = Dense(s * s, input_dim=self.latent_dim)(noise)
        x = Reshape((s, s, 1))(x)
        for i in reversed(range(self.config['model']['conv_blocks'])):
            x = Conv2DTranspose(8 * 2 ** i, (3, 3), padding='same')(x)
            x = LeakyReLU(alpha=0.2)(x)
            x = BatchNormalization(momentum=0.8)(x)
            x = UpSampling2D((2, 2))(x)
        img = Conv2DTranspose(self.channels, (3, 3), padding='same', activation='sigmoid')(x)

        # return model
        # model.summary()

        # noise = Input(shape=(self.latent_dim,))
        # img = model(noise)

        return Model(noise, img, name='generator')

    def build_discriminator(self):

        # model = Sequential(name='discriminator')
        img = Input(shape=self.img_shape)
        x = img
        for i in range(self.config['model']['conv_blocks']):
            x = Conv2D(8 * 2 ** i, (3, 3), padding='same')(x)
            x = LeakyReLU(alpha=0.2)(x)
            x = MaxPooling2D((2, 2))(x)
        x = Flatten()(x)
        x = Dense(self.config['model']['feature_dim'], name="features")(x)
        x = LeakyReLU(alpha=0.2)(x)
        validity = Dense(1, activation='sigmoid')(x)
        # model.summary()

        # img = Input(shape=self.img_shape)
        # validity = model(img)

        # return model
        return Model(img, validity, name='discriminator')

    def train(self, epochs, steps_per_epoch, init_epoch):
        sess = tf.Session()
        crop_generator = self.crop_generator.generate(sess)
        for epoch in range(init_epoch, epochs + 1):
            for step in range(steps_per_epoch):
                crops = next(crop_generator)[0]
                batch_size = crops.shape[0]

                valid = np.ones((batch_size, 1))
                fake = np.zeros((batch_size, 1))

                # ---------------------
                #  Train Discriminator
                # ---------------------

                noise = np.random.normal(0, 1, (batch_size, self.latent_dim))

                # Generate a batch of new images
                gen_crops = self.generator.predict(noise)

                # Train the discriminator
                d_loss_real = self.discriminator.train_on_batch(crops, valid)
                d_loss_fake = self.discriminator.train_on_batch(gen_crops, fake)
                d_loss = 0.5 * np.add(d_loss_real, d_loss_fake)

                # ---------------------
                #  Train Generator
                # ---------------------

                noise = np.random.normal(0, 1, (batch_size, self.latent_dim))

                # Train the generator (to have the discriminator label samples as valid)
                g_loss = self.combined.train_on_batch(noise, valid)

                # Plot the progress
                print("Epoch %d [D loss: %f, acc.: %.2f%%] [G loss: %f]" % (epoch, d_loss[0], 100*d_loss[1], g_loss))
            filename_d = os.path.join(self.config['training']['output'], '{}_epoch_{}'.format('discriminator', epoch))
            filename_g = os.path.join(self.config['training']['output'], '{}_epoch_{}'.format('generator', epoch))
            self.discriminator.save_weights(filename_d)
            self.generator.save_weights(filename_g)


class ModelClass(DeepProfilerModel):
    def __init__(self, config, dset, generator, val_generator):
        super(ModelClass, self).__init__(config, dset, generator, val_generator)
        self.gan = GAN(config, self.train_crop_generator, self.val_crop_generator)
        self.feature_model = self.gan.discriminator

    def train(self, epoch=1, metrics=['accuracy']):
        print(self.gan.combined.summary())
        if not os.path.isdir(self.config["training"]["output"]):
            os.mkdir(self.config["training"]["output"])
        if not os.path.isdir(os.path.join(self.config["training"]["output"], "checkpoints")):
            os.mkdir(os.path.join(self.config["training"]["output"], "checkpoints"))
        if self.config["model"]["comet_ml"]:
            experiment = Experiment(
                api_key=self.config["validation"]["api_key"],
                project_name=self.config["validation"]["project_name"]
            )
        # Create cropping graph
        crop_graph = tf.Graph()
        with crop_graph.as_default():
            cpu_config = tf.ConfigProto(device_count={'CPU': 1, 'GPU': 0})
            cpu_config.gpu_options.visible_device_list = ""
            crop_session = tf.Session(config=cpu_config)
            self.train_crop_generator.start(crop_session)
        gc.collect()

        configuration = tf.ConfigProto()
        configuration.gpu_options.visible_device_list = self.config["training"]["visible_gpus"]

        # # Start validation session
        # crop_graph = tf.Graph()
        # with crop_graph.as_default():
        #     val_session = tf.Session(config=configuration)
        #     keras.backend.set_session(val_session)
        #     self.val_crop_generator.start(val_session)
        #     x_validation, y_validation = deepprofiler.learning.validation.validate(  # TODO
        #         self.config,
        #         self.dset,
        #         self.val_crop_generator,
        #         val_session)
        # gc.collect()
        # Start main session
        main_session = tf.Session(config=configuration)
        keras.backend.set_session(main_session)

        # output_file = self.config["training"]["output"] + "/checkpoint_{epoch:04d}.hdf5"
        # callback_model_checkpoint = keras.callbacks.ModelCheckpoint(
        #     filepath=output_file,
        #     save_weights_only=True,
        #     save_best_only=False
        # )
        # csv_output = self.config["training"]["output"] + "/log.csv"
        # callback_csv = keras.callbacks.CSVLogger(filename=csv_output)

        # callbacks = [callback_model_checkpoint, callback_csv]  # TODO

        discriminator_file = os.path.join(self.config["training"]["output"], "discriminator_epoch_{}".format(epoch - 1))
        generator_file = os.path.join(self.config["training"]["output"], "generator_epoch_{}".format(epoch - 1))
        if epoch >= 1 and os.path.isfile(discriminator_file) and os.path.isfile(generator_file):
            self.gan.discriminator.load_weights(discriminator_file)
            self.gan.generator.load_weights(generator_file)
            print("Weights from previous models loaded:", discriminator_file, generator_file)

        epochs = self.config["training"]["epochs"]
        steps = self.config["training"]["steps"]

        if self.config["model"]["comet_ml"]:
            params = self.config["model"]["params"]
            experiment.log_multiple_params(params)

        keras.backend.get_session().run(tf.initialize_all_variables())
        self.gan.train(epochs, steps, epoch)

        # Close session and stop threads
        print("Complete! Closing session.", end="", flush=True)
        self.train_crop_generator.stop(crop_session)
        crop_session.close()
        print("All set.")
        gc.collect()
