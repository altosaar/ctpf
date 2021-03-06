"""

Poisson matrix factorization with Batch inference

CREATED: 2014-03-25 02:06:52 by Dawen Liang <dliang@ee.columbia.edu>

"""
import logging
import numpy as np
from scipy import sparse, special, weave

from sklearn.base import BaseEstimator, TransformerMixin


class PoissonMF(BaseEstimator, TransformerMixin):
    ''' Poisson matrix factorization with batch inference '''
    def __init__(self, n_components=100, max_iter=100, min_iter=1, tol=0.0001,
                 smoothness=100, random_state=None, verbose=False,
                 items_init_scale=1, **kwargs):
        ''' Poisson matrix factorization

        Arguments
        ---------
        n_components : int
            Number of latent components

        max_iter : int
            Maximal number of iterations to perform

        min_iter : int
            Minimum number of iterations to perform before checking for tolerance

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
        self.min_iter = min_iter
        self.tol = tol
        self.smoothness = smoothness
        self.items_init_scale = items_init_scale
        self.random_state = random_state
        self.verbose = verbose
        self.max_iter_fixed = 10 # max number of times to switch between fixed user udpates and fixed item updates

        if type(self.random_state) is int:
            np.random.seed(self.random_state)
        elif self.random_state is not None:
            np.random.setstate(self.random_state)

        self._parse_args(**kwargs)

    def _parse_args(self, **kwargs):
        self.a = float(kwargs.get('a', 0.1))
        self.b = float(kwargs.get('b', 0.1))
        self.c = float(kwargs.get('c', 0.1))
        self.d = float(kwargs.get('d', 0.1))

    def _init_users(self, n_users, theta=False):
        if type(theta) == np.ndarray:
            self.logger.info('initializing theta to be the observed one')
            self.Et = theta
            self.Elogt = None
            self.gamma_t = None
            self.rho_t = None
        else:
            # variational parameters for theta
            self.gamma_t = self.smoothness * \
                np.random.gamma(self.smoothness, 1. / self.smoothness,
                                size=(self.n_components, n_users)
                                ).astype(np.float32)
            self.rho_t = self.smoothness * \
                np.random.gamma(self.smoothness, 1. / self.smoothness,
                                size=(self.n_components, n_users)
                                ).astype(np.float32)
            self.Et, self.Elogt = _compute_expectations(self.gamma_t, self.rho_t)

    def _init_items(self, n_items, beta=False, categorywise=False):
        # if we pass in observed betas:
        if type(beta) == np.ndarray and not categorywise:
            self.logger.info('initializing beta to be the observed one')
            self.Eb = beta
            self.Elogb = None
            self.gamma_b = None
            self.rho_b = None
        else: # proceed normally
            self.logger.info('initializing normal variational params')
            # variational parameters for beta
            self.gamma_b = self.items_init_scale * self.smoothness * \
                np.random.gamma(self.smoothness, 1. / self.smoothness,
                                size=(n_items, self.n_components)
                                ).astype(np.float32)
            self.rho_b = self.items_init_scale * self.smoothness * \
                np.random.gamma(self.smoothness, 1. / self.smoothness,
                                size=(n_items, self.n_components)
                                ).astype(np.float32)
            self.Eb, self.Elogb = _compute_expectations(self.gamma_b, self.rho_b)

    def fit(self, X, rows, cols, vad,
        beta=False, theta=False, categorywise=False, item_fit_type='default',
        user_fit_type='default',
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
        self.n_users = n_users
        if type(theta) == np.ndarray:
            observed_user_preferences = True

        self._init_items(n_items, beta=beta, categorywise=categorywise)
        self._init_users(n_users, theta=theta)
        if user_fit_type != 'default':
            best_validation_ll = -np.inf
            for switch_idx in xrange(self.max_iter_fixed):
                if user_fit_type == 'converge_separately':
                    if switch_idx % 2 == 0:
                        only_update = 'items'
                    else:
                        only_update = 'users'

                    if switch_idx == 1:
                        initialize_users = 'default'
                    else:
                        initialize_users = 'none'
                self.logger.info('=> only updating {}, switch number {}'
                    .format(only_update, switch_idx))
                validation_ll, best_pll_dict = self._update(X, rows, cols, vad, beta=beta,
                    theta=theta,
                    observed_user_preferences=observed_user_preferences,
                    categorywise=categorywise,
                    user_fit_type=user_fit_type,
                    item_fit_type=item_fit_type,
                    initialize_users = initialize_users,
                    zero_untrained_components=zero_untrained_components,
                    only_update=only_update)
                # set to best run
                new_validation_ll = best_pll_dict['pred_ll']
                self.logger.info('set params to best pll {}, old one was {}'
                    .format(new_validation_ll, validation_ll))
                validation_ll = new_validation_ll
                self.Eb = best_pll_dict['best_Eb']
                self.Et = best_pll_dict['best_Et']
                self.Elogb = best_pll_dict['best_Elogb']
                self.Elogt = best_pll_dict['best_Elogt']
                if validation_ll > best_validation_ll:
                    best_Eb = self.Eb
                    # print best_Eb
                    # print '^Eb'
                    best_Et = self.Et
                    # print best_Et
                    # print '^Et'
                    # best_self = self
                    best_validation_ll = validation_ll
            # self = best_self
            self.logger.info('best validation ll was {}'.format(best_validation_ll))
            self.Eb = best_Eb
            self.Et = best_Et
        else:
            _, _ = self._update(X, rows, cols, vad, beta=beta,
                                categorywise=categorywise,
                                user_fit_type=user_fit_type,
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

    def _update(self, X, rows, cols, vad, beta=False,
        theta=False,
        observed_user_preferences=False,
        categorywise=False,
        item_fit_type='default',
        user_fit_type='default',
        update='default',
        zero_untrained_components=False,
        initialize_users='none',
        only_update=None):
        # alternating between update latent components and weights
        old_pll = -np.inf
        best_pll_dict = dict(pred_ll = -np.inf)
        for i in xrange(self.max_iter):
            # if user prefs observed, do nothing
            if (only_update == 'items' or observed_user_preferences and
                update != 'default'):
                pass
            elif (only_update == 'users'):
                if initialize_users == 'default':
                    if i == 0:
                        self.logger.info('initializing default users')
                        self._init_users(self.n_users)
                    self._update_users(X, rows, cols, beta=False,
                        observed_user_preferences=False,
                        only_update=only_update)
                elif initialize_users == 'trained':
                    if i == 0:
                        self.logger.info('updating users with trained prefs')
                        self._update_users(X, rows, cols, beta=beta,
                            theta=theta,
                            observed_user_preferences=True,
                            observed_item_attributes=False,
                            only_update=only_update)
                    if i > 0:
                        self._update_users(X, rows, cols, beta=beta,
                            theta=theta,
                            observed_user_preferences=False,
                            observed_item_attributes=False,
                            only_update=only_update)
                elif initialize_users == 'none':
                    self._update_users(X, rows, cols, beta=False,
                        observed_user_preferences=False,
                        only_update=only_update)
            else:
                self._update_users(X, rows, cols, beta=beta)

            if (type(beta) == np.ndarray and not categorywise or
                update == 'users' or only_update == 'users'):
                # do nothing if we have observed betas or are only updating users
                pass
            elif item_fit_type != 'default':
                if zero_untrained_components and i == 0 and update == 'default':
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
                            observed_user_preferences=observed_user_preferences,
                            categorywise=categorywise, iteration=i,
                            update='in_category')
                    else:
                        self._update_items(X, rows, cols, beta=beta,
                            observed_user_preferences=observed_user_preferences,
                            categorywise=categorywise, iteration=i,
                            update='out_category')
                elif (type(beta) == np.ndarray and categorywise and
                    item_fit_type == 'converge_in_category_first'):
                    # first update in-category components
                    if update == 'default':
                        self._update_items(X, rows, cols, beta=beta,
                            observed_user_preferences=observed_user_preferences,
                            categorywise=categorywise, update='in_category')
                    else:
                        self._update_items(X, rows, cols, beta=beta,
                            observed_user_preferences=observed_user_preferences,
                            categorywise=categorywise, update=update)
                elif (type(beta) == np.ndarray and categorywise and
                    item_fit_type == 'converge_out_category_first'):
                    # first update out-category components
                    if update == 'default':
                        self._update_items(X, rows, cols, beta=beta,
                            observed_user_preferences=observed_user_preferences,
                            categorywise=categorywise, update='out_category')
                    else:
                        self._update_items(X, rows, cols, beta=beta,
                            observed_user_preferences=observed_user_preferences,
                            categorywise=categorywise, update=update)
            else:
                self._update_items(X, rows, cols)
            pred_ll = self.pred_loglikeli(**vad)
            if np.isnan(pred_ll):
                self.logger.error('got nan in predictive ll')
                raise Exception('nan in predictive ll')
            else:
                if pred_ll > best_pll_dict['pred_ll']:
                    best_pll_dict['pred_ll'] = pred_ll
                    self.logger.info('logged new best pred_ll as {}'
                        .format(pred_ll))
                    best_pll_dict['best_Eb'] = self.Eb
                    best_pll_dict['best_Elogb'] = self.Elogb
                    best_pll_dict['best_Et'] = self.Et
                    best_pll_dict['best_Elogt'] = self.Elogt
            improvement = (pred_ll - old_pll) / abs(old_pll)
            if self.verbose:
                string = 'ITERATION: %d\tPred_ll: %.2f\tOld Pred_ll: %.2f\t Improvement: %.5f' % (i, pred_ll, old_pll, improvement)
                self.logger.info(string)
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
                            observed_user_preferences=observed_user_preferences,
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
                            observed_user_preferences=observed_user_preferences,
                            categorywise=categorywise, item_fit_type=item_fit_type,
                            update='in_category')
                    # if user_fit_type == 'converge_separately':
                    #     self._update(X, rows, cols, vad, beta=beta,
                    #         observed_user_preferences=observed_user_preferences,
                    #         user_fit_type=user_fit_type,
                    #         categorywise=categorywise,
                    #         item_fit_type=item_fit_type)
                break
            old_pll = pred_ll
        #pass
        return pred_ll, best_pll_dict #return the validation ll

    def _update_users(self, X, rows, cols, beta=False, theta=False,
        observed_user_preferences=False, observed_item_attributes=False,
        only_update=False):

        xexplog = self._xexplog(rows, cols, beta=beta,
            observed_user_preferences=observed_user_preferences)

        self.logger.info('updating users')

        ratioT = sparse.csr_matrix(( X.data / xexplog,
                                    (rows, cols)),
                                   dtype=np.float32, shape=X.shape).transpose()
        if observed_user_preferences:
            self.logger.info('updating user preferences based on fixed values')
            expLogElogt = self.Et
        else:
            expLogElogt = np.exp(self.Elogt)

        if type(beta) == np.ndarray or only_update == 'users':
            self.gamma_t = self.a + expLogElogt * \
                ratioT.dot(self.Eb).T
        else:
            self.gamma_t = self.a + expLogElogt * \
                ratioT.dot(np.exp(self.Elogb)).T

        self.rho_t = self.b + np.sum(self.Eb, axis=0, keepdims=True).T
        self.Et, self.Elogt = _compute_expectations(self.gamma_t, self.rho_t)

    def _update_items(self, X, rows, cols, beta=False, categorywise=False,
        observed_user_preferences=False,
        iteration=None, update='default'):

        self.logger.info('updating items')

        xexplog = self._xexplog(rows, cols,
            observed_user_preferences=observed_user_preferences)

        ratio = sparse.csr_matrix((X.data / xexplog,
                                   (rows, cols)),
                                  dtype=np.float32, shape=X.shape)
        if (type(beta) == np.ndarray and
                categorywise and
                update != 'default'):

            beta_bool = beta.astype(bool)
            if observed_user_preferences:
                gamma_b_updated = self.c + np.exp(self.Elogb) * \
                    ratio.dot(self.Et.T)
            else:
                gamma_b_updated = self.c + np.exp(self.Elogb) * \
                    ratio.dot(np.exp(self.Elogt.T))
            rho_b_updated = self.d + np.sum(self.Et, axis=1)
            rho_b_updated_reshaped = np.reshape(np.repeat(rho_b_updated,
                self.rho_b.shape[0], axis=0), self.rho_b.shape)
            if update == 'in_category':
                    self.logger.info('updating *only* in-category parameters')
                    self.gamma_b[beta_bool] = gamma_b_updated[beta_bool]
                    self.rho_b[beta_bool] = rho_b_updated_reshaped[beta_bool]
            elif update == 'out_category':
                    beta_bool_not = np.logical_not(beta_bool)
                    self.logger.info('updating *only* out-category parameters')
                    self.gamma_b[beta_bool_not] = gamma_b_updated[beta_bool_not]
                    self.rho_b[beta_bool_not] = \
                        rho_b_updated_reshaped[beta_bool_not]
        else:
            self.gamma_b = self.c + np.exp(self.Elogb) * \
                ratio.dot(np.exp(self.Elogt.T))
            self.rho_b = self.d + np.sum(self.Et, axis=1)
        self.Eb, self.Elogb = _compute_expectations(self.gamma_b, self.rho_b)

    def _xexplog(self, rows, cols, beta=False, observed_item_attributes=False,
        observed_user_preferences=False):
        '''
        sum_k exp(E[log theta_{ik} * beta_{kd}])
        '''
        if type(beta) == np.ndarray and observed_item_attributes:
            # add trick for log sum exp overflow prevention
            #data = _inner(self.Eb, np.exp(self.Elogt - self.Elogt.max()), rows, cols)
            data = _inner(self.Eb, np.exp(self.Elogt), rows, cols)
        elif observed_user_preferences:
            data = _inner(np.exp(self.Elogb), self.Et, rows, cols)
        else:
            data = _inner(np.exp(self.Elogb), np.exp(self.Elogt), rows, cols)
        return data

    def pred_loglikeli(obj, X_new, rows_new, cols_new):
        X_pred = _inner(obj.Eb, obj.Et, rows_new, cols_new)
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
