"""

Hierarchical Poisson matrix factorization with Batch inference

CREATED: 2014-11-17 16:06:50 by Dawen Liang <dliang@ee.columbia.edu>

"""

import sys
import numpy as np
from scipy import sparse, special, weave
import logging

from sklearn.base import BaseEstimator, TransformerMixin


class HPoissonMF(BaseEstimator, TransformerMixin):
    ''' Hierarchical Poisson matrix factorization with batch inference '''
    def __init__(self, n_components=100, max_iter=100, min_iter=1, tol=0.0001,
                 smoothness=100, random_state=None, verbose=False,
                 **kwargs):
        ''' Hierarchical Poisson matrix factorization

        Arguments
        ---------
        n_components : int
            Number of latent components

        max_iter : int
            Maximal number of iterations to perform

        tol : float
            The threshold on the increase of the objective to stop the
            iteration

        smoothness : int
            Smoothness on the initialization variational parameters

        random_state : int or RandomState
            Pseudo random number generator used for sampling

        verbose : bool
            Whether to show progress during model fitting

        **kwargs: dict
            Model hyperparameters
        '''
        self.logger = logging.getLogger(__name__)

        self.n_components = n_components
        self.max_iter = max_iter
        self.tol = tol
        self.smoothness = smoothness
        self.random_state = random_state
        self.verbose = verbose
        self.min_iter = min_iter

        if type(self.random_state) is int:
            np.random.seed(self.random_state)
        elif self.random_state is not None:
            np.random.setstate(self.random_state)

        self._parse_args(**kwargs)

    def _parse_args(self, **kwargs):
        self.a = float(kwargs.get('a', 0.1))
        self.c = float(kwargs.get('c', 0.1))
        self.a_ksi = float(kwargs.get('a_ksi', 0.1))
        self.b_ksi = float(kwargs.get('b_ksi', 0.1))
        self.c_eta = float(kwargs.get('c_eta', 0.1))
        self.d_eta = float(kwargs.get('d_eta', 0.1))

    def _init_users(self, n_users):
        # variational parameters for user factor theta
        self.gamma_t = self.smoothness * \
            np.random.gamma(self.smoothness, 1. / self.smoothness,
                            size=(self.n_components, n_users)
                            ).astype(np.float32)
        self.rho_t = self.smoothness * \
            np.random.gamma(self.smoothness, 1. / self.smoothness,
                            size=(self.n_components, n_users)
                            ).astype(np.float32)
        self.Et, self.Elogt = _compute_expectations(self.gamma_t, self.rho_t)
        # variational parameters for user activity
        self.gamma_ksi = self.smoothness * \
            np.random.gamma(self.smoothness, 1. / self.smoothness,
                            size=n_users).astype(np.float32)
        self.rho_ksi = self.smoothness * \
            np.random.gamma(self.smoothness, 1. / self.smoothness,
                            size=n_users).astype(np.float32)
        self.Eksi, _ = _compute_expectations(self.gamma_ksi, self.rho_ksi)

    def _init_items(self, n_items, beta=False):
        # if observed cats:
        if type(beta) == np.ndarray:
            self.logger.info('initializing beta to be observed')
            self.Eb = beta
            self.Elogb = None
            self.gamma_b = None
            self.rho_b = None
            self.Eeta = None
            self.gamma_eta = None
            self.rho_eta = None
        else: #normal
            # variational parameters for item factor beta
            self.logger.info('initializing normal variational params')
            self.gamma_b = self.smoothness * \
                np.random.gamma(self.smoothness, 1. / self.smoothness,
                                size=(n_items, self.n_components)
                                ).astype(np.float32)
            self.rho_b = self.smoothness * \
                np.random.gamma(self.smoothness, 1. / self.smoothness,
                                size=(n_items, self.n_components)
                                ).astype(np.float32)
            self.Eb, self.Elogb = _compute_expectations(self.gamma_b, self.rho_b)
            # variational parameters for item popularity
            self.gamma_eta = self.smoothness * \
                np.random.gamma(self.smoothness, 1. / self.smoothness,
                                size=(n_items, 1)).astype(np.float32)
            self.rho_eta = self.smoothness * \
                np.random.gamma(self.smoothness, 1. / self.smoothness,
                                size=(n_items, 1)).astype(np.float32)
            self.Eeta, _ = _compute_expectations(self.gamma_eta, self.rho_eta)

    def fit(self, X, rows, cols, vad,
        beta=False, theta=False, categorywise=False, item_fit_type='default',
        zero_untrained_components=False):
        '''Fit the model to the data in X.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_feats)
            Training data.

        Returns
        -------
        self: object
            Returns the instance itself.
        '''
        n_items, n_users = X.shape
        self._init_items(n_items, beta=beta)
        self._init_users(n_users)
        self._update(X, rows, cols, vad, beta=beta, categorywise=categorywise,
            item_fit_type=item_fit_type,
            zero_untrained_components=zero_untrained_components)
        return self

    #def transform(self, X, attr=None):
    #    '''Encode the data as a linear combination of the latent components.

    #    Parameters
    #    ----------
    #    X : array-like, shape (n_samples, n_feats)

    #    attr: string
    #        The name of attribute, default 'Eb'. Can be changed to Elogb to
    #        obtain E_q[log beta] as transformed data.

    #    Returns
    #    -------
    #    X_new : array-like, shape(n_samples, n_filters)
    #        Transformed data, as specified by attr.
    #    '''

    #    if not hasattr(self, 'Eb'):
    #        raise ValueError('There are no pre-trained components.')
    #    n_samples, n_feats = X.shape
    #    if n_feats != self.Eb.shape[1]:
    #        raise ValueError('The dimension of the transformed data '
    #                         'does not match with the existing components.')
    #    if attr is None:
    #        attr = 'Et'
    #    self._init_weights(n_samples)
    #    self._update(X, update_beta=False)
    #    return getattr(self, attr)

    def _update(self, X, rows, cols, vad, beta=False, categorywise=False,
        item_fit_type='default', update='default', zero_untrained_components=False):
        # alternating between update latent components and weights
        old_pll = -np.inf
        for i in xrange(self.max_iter):
            self._update_users(X, rows, cols, beta=beta)
            if type(beta) == np.ndarray and not categorywise:
                pass
            elif item_fit_type != 'default':
                if zero_untrained_components and i == 1 and update == 'default':
                    # store the initial values somewhere, then zero them out,
                    # then load them back in once they've been fit
                    beta_bool = beta.astype(bool)
                    beta_bool_not = np.logical_not(beta_bool)
                    small_num = 1e-5
                    if item_fit_type == 'converge_in_category_first':
                        # zero out out_category components
                        gamma_b_out_category = self.gamma_b[beta_bool_not]
                        rho_b_out_category = self.rho_b[beta_bool_not]
                        self.gamma_b[beta_bool_not] = small_num
                        self.rho_b[beta_bool_not] = small_num
                    elif item_fit_type == 'converge_out_category_first':
                        # zero out in_category components
                        gamma_b_in_category = self.gamma_b[beta_bool]
                        rho_b_in_category = self.rho_b[beta_bool]
                        self.gamma_b[beta_bool] = small_num
                        self.rho_b[beta_bool] = small_num
                if (type(beta) == np.ndarray and categorywise and
                    item_fit_type == 'alternating_updates'):
                    # alternate between updating in-category and out-category components of items
                    if i % 2 == 0:
                        self._update_items(X, rows, cols, beta=beta,
                            categorywise=categorywise, iteration=i,
                            update='in_category')
                    else:
                        self._update_items(X, rows, cols, beta=beta,
                            categorywise=categorywise, iteration=i,
                            update='out_category')
                elif (type(beta) == np.ndarray and categorywise and
                    item_fit_type == 'converge_in_category_first'):
                    # first update in-category components
                    if update == 'default':
                        self._update_items(X, rows, cols, beta=beta,
                            categorywise=categorywise, update='in_category')
                    else:
                        self._update_items(X, rows, cols, beta=beta,
                            categorywise=categorywise, update=update)
                elif (type(beta) == np.ndarray and categorywise and
                    item_fit_type == 'converge_out_category_first'):
                    # first update out-category components
                    if update == 'default':
                        self._update_items(X, rows, cols, beta=beta,
                            categorywise=categorywise, update='out_category')
                    else:
                        self._update_items(X, rows, cols, beta=beta,
                            categorywise=categorywise, update=update)
            else:
                self._update_items(X, rows, cols)
            pred_ll = self.pred_loglikeli(**vad)
            if np.isnan(pred_ll):
                self.logger.error('got nan in predictive ll')
                raise Exception('nan in predictive ll')
            improvement = (pred_ll - old_pll) / abs(old_pll)
            if self.verbose:
                self.logger.info('ITERATION: %d\tPred_ll: %.2f\tOld Pred_ll: %.2f\t'
                      'Improvement: %.5f' % (i, pred_ll, old_pll, improvement))
                sys.stdout.flush()
            if improvement < self.tol and i > self.min_iter:
                if update == 'default' and item_fit_type != 'default':
                    if item_fit_type == 'converge_in_category_first':
                        # we converged in-category. now converge out_category
                        if zero_untrained_components:
                            self.logger.info(
                                're-load initial values for out_category')
                            self.gamma_b[beta_bool_not] = gamma_b_out_category
                            self.rho_b[beta_bool_not] = rho_b_out_category
                        self._update(X, rows, cols, vad, beta=beta,
                            categorywise=categorywise, item_fit_type=item_fit_type,
                            update='out_category')
                    if item_fit_type == 'converge_out_category_first':
                        # we converged out-category. now converge in_category
                        if zero_untrained_components:
                            self.logger.info(
                                're-load initial values for in_category')
                            self.gamma_b[beta_bool] = gamma_b_in_category
                            self.rho_b[beta_bool] = rho_b_in_category
                        self._update(X, rows, cols, vad, beta=beta,
                            categorywise=categorywise, item_fit_type=item_fit_type,
                            update='in_category')
                break
            old_pll = pred_ll
        pass

    def _update_users(self, X, rows, cols, beta=False, categorywise=False):
        xexplog = self._xexplog(rows, cols, beta=beta)
        ratioT = sparse.csr_matrix((X.data / xexplog,
                                    (rows, cols)),
                                   dtype=np.float32, shape=X.shape).transpose()
        if type(beta) == np.ndarray and not categorywise:
            self.gamma_t = self.a + np.exp(self.Elogt) * \
                ratioT.dot(self.Eb).T
        else:
            self.gamma_t = self.a + np.exp(self.Elogt) * \
                ratioT.dot(np.exp(self.Elogb)).T

        self.rho_t = self.Eksi + np.sum(self.Eb, axis=0, keepdims=True).T
        self.Et, self.Elogt = _compute_expectations(self.gamma_t, self.rho_t)

        # update user activity hyperprior
        self.gamma_ksi = self.a_ksi + self.n_components * self.a
        self.rho_ksi = self.b_ksi + np.sum(self.Et, axis=0)
        self.Eksi, _ = _compute_expectations(self.gamma_ksi, self.rho_ksi)

    def _update_items(self, X, rows, cols, beta=False, categorywise=False,
        update='default', iteration=None):
        ratio = sparse.csr_matrix((X.data / self._xexplog(rows, cols),
                                   (rows, cols)),
                                  dtype=np.float32, shape=X.shape)
        if type(beta) == np.ndarray and categorywise:
            beta_bool = beta.astype(bool)
            gamma_b_updated = self.c + np.exp(self.Elogb) * \
                ratio.dot(np.exp(self.Elogt.T))
            rho_b_updated = self.Eeta + np.sum(self.Et, axis=1)
            if update == 'in_category':
                self.logger.info('updating *only* in-category parameters')
                self.gamma_b[beta_bool] = gamma_b_updated[beta_bool]
                self.rho_b[beta_bool] = rho_b_updated[beta_bool]
            elif update == 'out_category':
                beta_bool_not = np.logical_not(beta_bool)
                self.logger.info('updating *only* out-category parameters')
                self.gamma_b[beta_bool_not] = gamma_b_updated[beta_bool_not]
                self.rho_b[beta_bool_not] = \
                    rho_b_updated[beta_bool_not]
            self.Eb, self.Elogb = _compute_expectations(self.gamma_b, self.rho_b)
        else:
            self.gamma_b = self.c + np.exp(self.Elogb) * \
                ratio.dot(np.exp(self.Elogt.T))
            self.rho_b = self.Eeta + np.sum(self.Et, axis=1)
            self.Eb, self.Elogb = _compute_expectations(self.gamma_b, self.rho_b)

        # update item popularity hyperprior, regardless
        self.gamma_eta = self.c_eta + self.n_components * self.c
        self.rho_eta = self.d_eta + np.sum(self.Eb, axis=1, keepdims=True)
        self.Eeta, _ = _compute_expectations(self.gamma_eta, self.rho_eta)

    def _xexplog(self, rows, cols, beta=False):
        '''
        sum_k exp(E[log theta_{ik} * beta_{kd}])
        '''
        if type(beta) == np.ndarray:
            data = _inner(self.Eb, np.exp(self.Elogt), rows, cols)
        else:
            data = _inner(np.exp(self.Elogb), np.exp(self.Elogt), rows, cols)
        return data

    def pred_loglikeli(self, X_new, rows_new, cols_new):
        X_pred = _inner(self.Eb, self.Et, rows_new, cols_new)
        pred_ll = np.mean(X_new * np.log(X_pred) - X_pred)
        return pred_ll


def _inner(beta, theta, rows, cols):
    n_ratings = rows.size
    n_components, n_users = theta.shape
    data = np.empty(n_ratings, dtype=np.float32)
    code = r"""
    for (int i = 0; i < n_ratings; i++) {
       data[i] = 0.0;
       for (int j = 0; j < n_components; j++) {
           data[i] += beta[rows[i] * n_components + j] * theta[j * n_users + cols[i]];
       }
    }
    """
    weave.inline(code, ['data', 'theta', 'beta', 'rows', 'cols',
                        'n_ratings', 'n_components', 'n_users'])
    return data


def _compute_expectations(alpha, beta):
    '''
    Given x ~ Gam(alpha, beta), compute E[x] and E[log x]
    '''
    return (alpha / beta, special.psi(alpha) - np.log(beta))
