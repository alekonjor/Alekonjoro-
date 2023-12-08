import abc
import typing
from dataclasses import dataclass
from typing import Any, NamedTuple, Optional, TypeVar, runtime_checkable

import equinox as eqx
import jax
import jaxtyping
import optax
from jax import numpy as jnp
from jax.random import PRNGKey
from jaxtyping import PRNGKeyArray

# TODO: remove dependency on _src internals
from optax._src import numerics
from optax._src.transform import bias_correction, update_moment

import levanter.tracker
from levanter.optim.config import HessianOptConfig, OptimizerConfig
from levanter.optim.second_order import SecondOrderTransformation, chain_second_order, inject_hyperparams
from levanter.optim.util import hvp, tree_gaussian
from levanter.utils.jax_utils import parameter_count


M = TypeVar("M")
Ex = TypeVar("Ex")

GAMMA_SOPHIA_G = 0.05
GAMMA_SOPHIA_H = 0.01


class ScaleBySophiaState(NamedTuple):
    """State for Sophia and similar."""

    count: jaxtyping.Array  # shape=(), dtype=jnp.int32.
    hessian_count: jaxtyping.Array  # shape=(), dtype=jnp.int32.
    mu: optax.Updates  # momentum
    h: optax.Updates  # EMA of hessian diagonal
    hess_key: PRNGKey


@runtime_checkable
class SophiaGObjective(typing.Protocol):
    """
    Class for objective functions that can be used with Sophia-G

    Sophia-G is a second order optimizer that uses the Gauss-Newton-Bartlett approximation to the Hessian
    to compute the second order update. This requires the objective function be of the form loss(logits(x))
    where logits(x) is the activation of the model for the given example x. This is the case for most models
    that are trained with "typical" losses.
    """

    def logits(self, parameters: M, example: Ex, *args, **kwargs) -> Any:
        """
        Returns the logits/activations of the model for the given example,
        or just sufficient statistics for the example for non-categorical models.
        """
        ...

    def sample(self, logits, example: Ex, *, key: PRNGKey) -> Ex:
        """
        Samples a new example with the same shape as the original example, but with
        the "labels" replaced with some sampled values
        """
        ...

    def loss(self, logits, example: Ex):
        """
        Just computes the loss, e.g. cross entropy.

        Should return the mean loss over the batch, not the sum.

        TODO: should we reconsider this?
        """
        ...

    def __call__(self, parameters: M, example: Ex, *args, **kwargs):
        """
        Just a convenience method for invoking the objective for "normal" training w/o sophia-g
        """
        logits = self.logits(parameters, example, *args, **kwargs)
        return self.loss(logits, example)

    def num_data_points(self, example: Ex) -> int:
        """
        Returns the number of data points in the example. This should take into account the loss mask
        or any other masking that might be applied to the example.

        By default, we just return 1, and you can just pull the term into the hyperparams of Sophia if you want.

        Returns:
               The number of data points in the example
        """
        return 1


@dataclass
class BaseSophiaConfig(HessianOptConfig):
    """Base class for sophia variants. Doesn't implement the state update"""

    weight_decay: float = 0.1
    beta1: float = 0.96
    beta2: float = 0.99

    epsilon: float = 1e-12
    clip_threshold: Optional[float] = 1.0
    rng_seed: int = 0

    @abc.abstractmethod
    def compute_hessian(
        self,
        fn,
        model,
        *batch,
        hess_key: PRNGKey,
        **batch_kwargs,
    ):
        raise NotImplementedError

    def build(self, num_train_steps: int):
        def _optimizer(learning_rate, gamma) -> SecondOrderTransformation:
            components = []
            key = jax.random.PRNGKey(self.rng_seed)

            components.append(
                _sophia_gradient_transform(
                    sophia_hess_fn=self.compute_hessian,
                    update_interval=self.update_interval,
                    b1=self.beta1,
                    b2=self.beta2,
                    eps=self.epsilon,
                    gamma=gamma,
                    initial_key=key,
                    clip_threshold=self.clip_threshold,
                )
            )

            # Algorithm 3, step 11 (Note, this comes after clipping b/c it's not supposed to be clipped)
            # In the paper, it comes as a prior step, but doesn't get clipped
            if self.weight_decay > 0:
                components.append(optax.add_decayed_weights(self.weight_decay))

            # - learning rate for descent
            components.append(optax.scale(-learning_rate))

            optimizer = chain_second_order(*components)

            return optimizer

        # Hong suggested using cosine decay for gamma
        # gamma_decay_schedule = optax.cosine_decay_schedule(self.gamma, num_train_steps // 2, 0)  # type: ignore
        constant_gamma_schedule = optax.constant_schedule(self.gamma)  # type: ignore
        # gamma_schedule = optax.join_schedules([constant_gamma_schedule, gamma_decay_schedule], [num_train_steps // 2])

        return inject_hyperparams(_optimizer)(
            learning_rate=self.lr_scheduler(num_train_steps), gamma=constant_gamma_schedule
        )


@OptimizerConfig.register_subclass("sophia-g")
@dataclass
class SophiaGConfig(BaseSophiaConfig):
    gamma: float = GAMMA_SOPHIA_G

    def compute_hessian(self, fn, model, *batch, hess_key: PRNGKey, **batch_kwargs):
        return stochastic_diag_gauss_newton(fn, model, *batch, **batch_kwargs, hess_key=hess_key)


@OptimizerConfig.register_subclass("sophia-h")
@dataclass
class SophiaHConfig(BaseSophiaConfig):
    gamma: float = GAMMA_SOPHIA_H

    def compute_hessian(self, fn, model, *batch, hess_key: PRNGKey, **batch_kwargs):
        return stochastic_hessian_diagonal(fn, model, *batch, **batch_kwargs, hess_key=hess_key)


def sophia_h(
    lr: float = 0.85e-3,
    *,
    b1: float = 0.965,
    b2: float = 0.99,
    eps: float = 1e-8,
    gamma: float = GAMMA_SOPHIA_H,
    weight_decay: float = 0.0,
    clip_threshold: Optional[float] = 1.0,
    update_interval: int = 10,
    key: PRNGKey,
) -> SecondOrderTransformation:
    """Sophia-H: https://arxiv.org/pdf/2305.14342.pdf Algorithm 1&3"""
    components = []

    components.append(scale_by_sophia_h(b1, b2, eps, gamma, clip_threshold, update_interval, key=key))

    if weight_decay > 0:
        components.append(optax.add_decayed_weights(weight_decay))

    components.append(optax.scale(-lr))

    return chain_second_order(*components)


def scale_by_sophia_h(
    b1=0.965,
    b2=0.99,
    eps=1e-8,
    gamma=GAMMA_SOPHIA_H,
    clip_threshold: Optional[float] = 1.0,
    update_interval=10,
    *,
    key: PRNGKey,
):

    return _sophia_gradient_transform(
        sophia_hess_fn=stochastic_hessian_diagonal,
        update_interval=update_interval,
        b1=b1,
        b2=b2,
        eps=eps,
        gamma=gamma,
        clip_threshold=clip_threshold,
        initial_key=key,
    )


def sophia_g(
    lr: float = 1e-3,
    *,
    b1: float = 0.99,
    b2: float = 0.99,
    eps: float = 1e-8,
    gamma: float = GAMMA_SOPHIA_G,
    weight_decay: float = 0.0,
    clip_threshold: Optional[float] = 1.0,
    update_interval: int = 10,
    key: PRNGKey,
) -> SecondOrderTransformation:
    """Sophia-G: https://arxiv.org/pdf/2305.14342.pdf Algorithm 2&3"""
    components = []

    components.append(scale_by_sophia_g(b1, b2, eps, gamma, clip_threshold, update_interval, key=key))

    if weight_decay > 0:
        components.append(optax.add_decayed_weights(weight_decay))

    components.append(optax.scale(-lr))

    return chain_second_order(*components)


def scale_by_sophia_g(
    b1: float = 0.99,
    b2: float = 0.99,
    eps: float = 1e-8,
    gamma: float = GAMMA_SOPHIA_G,
    clip_threshold: Optional[float] = 1.0,
    update_interval=10,
    *,
    key: PRNGKeyArray,
):

    return _sophia_gradient_transform(
        sophia_hess_fn=stochastic_diag_gauss_newton,
        update_interval=update_interval,
        b1=b1,
        b2=b2,
        eps=eps,
        gamma=gamma,
        clip_threshold=clip_threshold,
        initial_key=key,
    )


def _sophia_gradient_transform(
    sophia_hess_fn,
    update_interval: int,
    b1: float,
    b2: float,
    eps: float,
    gamma: float,
    clip_threshold: Optional[float],
    initial_key: PRNGKeyArray,
    mu_dtype: Optional[Any] = None,
) -> SecondOrderTransformation:
    mu_dtype = jax.canonicalize_dtype(mu_dtype) if mu_dtype is not None else None

    def init_fn(params):
        mu = jax.tree_util.tree_map(lambda t: jnp.zeros_like(t, dtype=mu_dtype), params)  # First moment
        h = jax.tree_util.tree_map(jnp.zeros_like, params)  # Second moment
        return ScaleBySophiaState(
            count=jnp.zeros([], jnp.int32), hessian_count=jnp.zeros([], jnp.int32), mu=mu, h=h, hess_key=initial_key
        )

    def update_fn(updates, state, params=None):
        mu = update_moment(updates, state.mu, b1, 1)
        # nu = update_moment_per_elem_norm(updates, state.nu, b2, 2)
        count_inc = numerics.safe_int32_increment(state.count)
        mu_hat = bias_correction(mu, b1, count_inc)
        h_hat = state.h
        # track how often hessian is used
        mu_leaves = jax.tree_util.tree_leaves(mu_hat)
        h_leaves = jax.tree_util.tree_leaves(h_hat)

        stats: dict[str, Any] = {
            "optim/param_norm": jnp.sqrt(sum(jnp.sum(p**2) for p in jax.tree_util.tree_leaves(params))),
            "optim/momentum_norm": jnp.sqrt(sum(jnp.sum(m**2) for m in mu_leaves)),
            "optim/hessian_norm": jnp.sqrt(sum(jnp.sum(h**2) for h in h_leaves)),
        }

        # with sophia-g the max(h, 0) is not needed but no harm
        updates = jax.tree_util.tree_map(
            # lambda m, v: m / jnp.maximum(jnp.maximum(jnp.abs(m), gamma * jnp.maximum(v, 0)), eps), mu_hat, h_hat
            lambda m, h: m / jnp.maximum(gamma * h, eps),
            mu_hat,
            h_hat,
        )

        if clip_threshold is not None:
            unclipped_count = sum(jnp.sum(jnp.abs(u) < clip_threshold) for u in jax.tree_util.tree_leaves(updates))
            updates = jax.tree_util.tree_map(lambda u: jnp.clip(u, -clip_threshold, clip_threshold), updates)
            stats["optim/unclipped_fraction"] = unclipped_count / parameter_count(updates)

        # this doesn't work well on CPU, so skip if cpu
        if jax.lib.xla_bridge.get_backend().platform != "cpu":
            levanter.tracker.jit_log(stats, step=state.count)

        if mu_dtype is not None:
            mu = jax.tree_util.tree_map(lambda t: t.astype(mu_dtype), mu)

        return updates, ScaleBySophiaState(
            count=count_inc, hessian_count=state.hessian_count, mu=mu, h=h_hat, hess_key=state.hess_key
        )

    def update_hessian(state, fn, model, *batch, **batch_kwargs):
        def _do_update():
            key, next_key = jax.random.split(state.hess_key)
            new_hess = sophia_hess_fn(fn, model, *batch, hess_key=key, **batch_kwargs)
            # new_hess = jax.tree_util.tree_map(lambda h: jnp.clip(h, -1, 1), new_hess)

            # EMAs of hessian
            hessian_count_inc = numerics.safe_int32_increment(state.hessian_count)
            nu = update_moment(new_hess, state.h, b2, 1)
            return ScaleBySophiaState(
                count=state.count, hessian_count=hessian_count_inc, mu=state.mu, h=nu, hess_key=next_key
            )

        def _dont_update():
            return state

        return jax.lax.cond(
            jnp.equal(state.count % update_interval, 0),
            lambda _: _do_update(),
            lambda _: _dont_update(),
            state.count,
        )

    return SecondOrderTransformation(init_fn, update_fn, update_hessian)


# use this for Sophia-G
def stochastic_diag_gauss_newton(fn: SophiaGObjective, model, example, *args, hess_key: PRNGKey, **kwargs):
    """

    Approximate the diagonal of the Hessian using an approximation to the Gauss Newton matrix.
    This is Algorithm 2 of https://arxiv.org/pdf/2305.14342.pdf

    Args:
        fn (SophiaGObjective): objective function
        model: model whose Hessian to compute
        hess_key: key for sampling
        *args, **kwargs: passed to fn's logits
    """
    if not isinstance(fn, SophiaGObjective):
        raise ValueError("objective must be a SophiaGObjective")

    # Step 3
    logits, model_backward = eqx.filter_vjp(lambda model: fn.logits(model, example, *args, **kwargs), model)

    # Step 4
    y_hat = fn.sample(logits, example, key=hess_key)

    # Step 5
    grad_loss_logits = eqx.filter_grad(fn.loss)(logits, y_hat)
    pseudo_g = model_backward(grad_loss_logits)[0]

    # Step 6
    bs = fn.num_data_points(example)
    h = jax.tree_util.tree_map(lambda x: x**2 * bs, pseudo_g)

    return h


# Use this for Sophia-H
def stochastic_hessian_diagonal(fn, model, *args, hess_key: PRNGKey, **kwargs):
    """Compute the diagonal of the Hessian of a function using a normal distribution.

    https://arxiv.org/pdf/2305.14342.pdf Algorithm 1

    Args:
        fn: function to compute the Hessian of
        model: model to compute the Hessian of
        hess_key: key for the normal distribution
    """
    # cf https://arxiv.org/pdf/2006.00719.pdf eqn 9
    # https://www-users.cse.umn.edu/~saad/PDF/umsi-2005-082.pdf
    # https://arxiv.org/pdf/2208.03268.pdf
    g = tree_gaussian(hess_key, model)
    # TODO: consider allowing for n > 1 gaussians?
    product = hvp(lambda m: fn(m, *args, **kwargs), model, g)
    hessian = jax.tree_util.tree_map(lambda grad, gaussian: grad * gaussian, product, g)

    return hessian
