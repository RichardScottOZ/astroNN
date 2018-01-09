# ---------------------------------------------------------------#
#   astroNN.models.loss.regression: loss function for regression
# ---------------------------------------------------------------#
import keras.backend as K


def mean_squared_error(y_true, y_pred):
    """
    NAME: mean_squared_error
    PURPOSE: calculate mean square error loss
    INPUT:
    OUTPUT:
    HISTORY:
        2017-Nov-16 - Written - Henry Leung (University of Toronto)
    """
    return K.mean(K.tf.where(K.tf.equal(y_true, -9999.), K.tf.zeros_like(y_true), K.square(y_true - y_pred)), axis=-1)


def mse_var_wrapper(lin):
    """
    NAME: mse_var_wrapper
    PURPOSE: calculate predictive variance
    INPUT:
    OUTPUT:
    HISTORY:
        2017-Nov-16 - Written - Henry Leung (University of Toronto)
    """
    def mse_var(y_true, y_pred):
        wrapper_output = K.tf.where(K.tf.equal(y_true, -9999.), K.tf.zeros_like(y_true),
                                    0.5 * K.square(y_true - lin) * (K.exp(-y_pred)) + 0.5 * y_pred)
        return K.mean(wrapper_output, axis=-1)

    return mse_var
