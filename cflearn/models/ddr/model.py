import torch

import numpy as np

from typing import Any
from typing import Dict
from typing import List
from typing import Union
from typing import Optional
from cftool.ml import Metrics
from cftool.misc import is_numeric
from cftool.misc import update_dict
from cftool.misc import timing_context
from cftool.misc import shallow_copy_dict
from cfdata.tabular import DataLoader

try:
    amp: Optional[Any] = torch.cuda.amp
except:
    amp = None

from ...misc.toolkit import *
from ...modules.blocks import *
from .core import DDRCore
from .loss import DDRLoss
from ..base import ModelBase
from ...types import tensor_dict_type

proj_type = Optional[Union[Linear, MLP]]
tensors_type = Union[
    torch.Tensor,
    List[torch.Tensor],
    Dict[str, Optional[Union[torch.Tensor, tensor_dict_type]]],
]


@ModelBase.register("ddr")
class DDR(ModelBase):
    def __init__(
        self,
        pipeline_config: Dict[str, Any],
        tr_loader: DataLoader,
        cv_loader: DataLoader,
        tr_weights: Optional[np.ndarray],
        cv_weights: Optional[np.ndarray],
        device: torch.device,
        *,
        use_tqdm: bool,
    ):
        if not tr_loader.data.task_type.is_reg:
            raise ValueError("DDR can only deal with regression problems")
        super().__init__(
            pipeline_config,
            tr_loader,
            cv_loader,
            tr_weights,
            cv_weights,
            device,
            use_tqdm=use_tqdm,
        )
        self.q_metric = Metrics("quantile")
        cfg = self.get_core_config(self)
        assert cfg.pop("out_dim") == 1
        self.core = DDRCore(**cfg)

    @property
    def input_sample(self) -> tensor_dict_type:
        return super().input_sample

    @staticmethod
    def get_core_config(instance: "ModelBase") -> Dict[str, Any]:
        cfg = ModelBase.get_core_config(instance)
        latent_dim = instance.config.setdefault("latent_dim", None)
        num_blocks = instance.config.setdefault("num_blocks", None)
        transition_builder = instance.config.setdefault("transition_builder", None)
        to_builder = instance.config.setdefault("to_transition_builder", None)
        from_builder = instance.config.setdefault("from_transition_builder", None)
        cfg.update(
            {
                "y_min": instance.y_min,
                "y_max": instance.y_max,
                "num_blocks": num_blocks,
                "latent_dim": latent_dim,
                "transition_builder": transition_builder,
                "to_transition_builder": to_builder,
                "from_transition_builder": from_builder,
            }
        )
        return cfg

    def _init_config(self) -> None:
        super()._init_config()
        # common
        self.config.setdefault("ema_decay", 0.999)
        step_per_epoch = len(self.tr_loader)
        self._synthetic_step = self.config.setdefault("synthetic_step", 10)
        self._synthetic_range = self.config.setdefault("synthetic_range", 3.0)
        labels = self.tr_data.processed.y
        self.y_min, self.y_max = labels.min(), labels.max()
        self.y_diff = self.y_max - self.y_min
        self._quantile_anchors = np.linspace(0.05, 0.95, 10).astype(np.float32)
        # loss config
        self._loss_config = self.config.setdefault("loss_config", {})
        self._loss_config.setdefault("mtl_method", None)
        # trainer config
        default_metric_types = [
            "ddr",
            "loss",
            "pdf",
            "cdf",
            "q_recover",
            "median_recover",
        ]
        trainer_config = self._pipeline_config.setdefault("trainer_config", {})
        trainer_config = update_dict(
            trainer_config,
            {
                "clip_norm": 1.0,
                "num_epoch": 40,
                "max_epoch": 1000,
                "metric_config": {"types": default_metric_types},
            },
        )
        self._pipeline_config["trainer_config"] = trainer_config
        self._trainer_config = trainer_config

    def _init_loss(self) -> None:
        loss_config = shallow_copy_dict(self._loss_config)
        self.loss = DDRLoss(loss_config, "none")

    # utilities

    def _convert_np_anchors(self, np_anchors: np.ndarray) -> torch.Tensor:
        tensor = to_torch(np_anchors.reshape([-1, 1]))
        return tensor.to(self.device).requires_grad_(True)

    def _expand(
        self,
        n: int,
        elem: Union[float, torch.Tensor],
        *,
        numpy: bool = False,
        to_device: bool = True,
    ) -> Union[torch.Tensor, np.ndarray]:
        if isinstance(elem, torch.Tensor):
            return elem
        if not is_numeric(elem):
            elem_arr = np.asarray(elem, np.float32)
        else:
            elem_arr = np.repeat(elem, n).astype(np.float32)
        elem_arr = elem_arr.reshape([-1, 1])
        if numpy:
            return elem_arr
        elem_tensor = torch.from_numpy(elem_arr)
        if to_device:
            elem_tensor = elem_tensor.to(self.device)
        return elem_tensor

    # core

    def _quantile(
        self,
        net: torch.Tensor,
        q_batch: Optional[torch.Tensor],
        do_inverse: bool,
    ) -> tensor_dict_type:
        if q_batch is None:
            results = self.core(net, median=True, do_inverse=do_inverse)
        else:
            results = self.core(net, q_batch=q_batch, do_inverse=do_inverse)
        return results

    def _median(self, net: torch.Tensor, do_inverse: bool) -> tensor_dict_type:
        return self._quantile(net, None, do_inverse)

    def _cdf(
        self,
        net: torch.Tensor,
        y_batch: torch.Tensor,
        need_optimize: bool,
        return_pdf: bool,
        do_inverse: bool,
    ) -> tensor_dict_type:
        y_batch.requires_grad_(return_pdf)
        with mode_context(self, to_train=None, use_grad=return_pdf):
            results = self.core(net, y_batch=y_batch, do_inverse=do_inverse)
        if not return_pdf:
            pdf = None
        else:
            cdf = results["q"]
            pdf = get_gradient(cdf, y_batch, need_optimize, need_optimize)
            assert isinstance(pdf, torch.Tensor)
            y_batch.requires_grad_(False)
        results["pdf"] = pdf
        return results

    def _core(
        self,
        net: torch.Tensor,
        batch_step: int,
        synthetic: bool,
    ) -> tensor_dict_type:
        # generate quantile / anchor batch
        with timing_context(self, "forward.generate_anchors"):
            if self.training:
                q_batch = np.random.random([len(net), 1]).astype(np.float32)
                y_batch = np.random.random([len(net), 1]).astype(np.float32)
            else:
                q_batch = y_batch = self._quantile_anchors
                n_repeat = int(len(net) / len(q_batch)) + 1
                q_batch = np.repeat(q_batch, n_repeat)[: len(net)]
                y_batch = np.repeat(y_batch, n_repeat)[: len(net)]
            y_batch = y_batch * self.y_diff + self.y_min
            q_batch = self._convert_np_anchors(q_batch)
            y_batch = self._convert_np_anchors(y_batch)
        # build predictions
        with timing_context(self, "forward.median"):
            if synthetic:
                median = median_inverse = None
            else:
                results = self._median(net, True)
                median = results["y"]
                median_inverse = results["q_inverse"]
        with timing_context(self, "forward.quantile"):
            quantile_results = self._quantile(net, q_batch, True)
            y = quantile_results["y"]
            q_inverse = quantile_results["q_inverse"]
        with timing_context(self, "forward.cdf"):
            cdf_inverse = not synthetic
            cdf_results = self._cdf(net, y_batch, True, True, cdf_inverse)
            cdf, cdf_logit, pdf = map(cdf_results.get, ["q", "q_logit", "pdf"])
            if not cdf_inverse:
                y_inverse = None
            else:
                y_inverse = cdf_results["y_inverse"]
        # construct results
        return {
            "net": net,
            "predictions": median,
            "median_inverse": median_inverse,
            "q_batch": q_batch,
            "y_batch": y_batch,
            "y": y,
            "q_inverse": q_inverse,
            "pdf": pdf,
            "cdf": cdf,
            "cdf_logit": cdf_logit,
            "y_inverse": y_inverse,
        }

    # API

    def forward(
        self,
        batch: tensor_dict_type,
        batch_indices: Optional[np.ndarray] = None,
        loader_name: Optional[str] = None,
        batch_step: int = 0,
        **kwargs: Any,
    ) -> tensor_dict_type:
        # pre-processing
        x_batch = batch["x_batch"]
        net = self._split_features(x_batch, batch_indices, loader_name).merge()
        if self.tr_data.is_ts:
            net = net.view(net.shape[0], -1)
        # check inference
        forward_dict = {}
        predict_pdf = kwargs.get("predict_pdf", False)
        predict_cdf = kwargs.get("predict_cdf", False)
        if predict_pdf or predict_cdf:
            y = kwargs.get("y")
            if y is None:
                raise ValueError(f"pdf / cdf cannot be predicted without y")
            y_batch = self._expand(len(net), y, numpy=True)
            y_batch = self.tr_data.transform_labels(y_batch)
            y_batch = to_torch(y_batch).to(self.device)
            results = self._cdf(net, y_batch, False, predict_pdf, False)
            if predict_pdf:
                forward_dict["pdf"] = results["pdf"]
            if predict_cdf:
                forward_dict["cdf"] = results["q"]
        predict_quantile = kwargs.get("predict_quantiles")
        if predict_quantile:
            q = kwargs.get("q")
            if q is None:
                raise ValueError(f"quantile cannot be predicted without q")
            q_batch = self._expand(len(net), q)
            forward_dict["quantiles"] = self._quantile(net, q_batch, False)["y"]
        if not forward_dict:
            forward_dict = self._core(net, batch_step, False)
        return forward_dict

    def loss_function(
        self,
        batch: tensor_dict_type,
        batch_indices: np.ndarray,
        forward_results: tensor_dict_type,
        batch_step: int,
    ) -> tensor_dict_type:
        y_batch = batch["y_batch"]
        losses, losses_dict = self.loss(forward_results, y_batch)
        net = forward_results["net"]
        if (
            self.training
            and self._synthetic_step > 0
            and batch_step % self._synthetic_step == 0
        ):
            with timing_context(self, "synthetic.forward"):
                net_min = torch.min(net, dim=0)[0]
                net_max = torch.max(net, dim=0)[0]
                net_diff = net_max - net_min
                diff_span = 0.5 * (self._synthetic_range - 1.0) * net_diff
                synthetic_net = net.new_empty(net.shape)
                synthetic_net.uniform_(0, 1)
                synthetic_net = self._synthetic_range * synthetic_net * net_diff
                synthetic_net = synthetic_net - (diff_span - net_min)
                # synthetic_net ~ U_[ -diff_span + min, diff_span + max ]
                synthetic_outputs = self._core(synthetic_net, batch_step, True)
            with timing_context(self, "synthetic.loss"):
                syn_losses, syn_losses_dict = self.loss._core(  # type: ignore
                    synthetic_outputs,
                    y_batch,
                    is_synthetic=True,
                )
            losses_dict.update(syn_losses_dict)
            losses = losses + syn_losses
        losses_dict["loss"] = losses
        losses_dict = {k: v.mean() for k, v in losses_dict.items()}
        if not self.training:
            assert isinstance(y_batch, torch.Tensor)
            q_losses = []
            y_batch = to_numpy(y_batch)
            for q in self._quantile_anchors:
                self.q_metric.config["q"] = q
                yq = self._quantile(net, self._expand(len(net), q), False)["y"]
                q_losses.append(self.q_metric.metric(y_batch, to_numpy(yq)))
            quantile_metric = -sum(q_losses) / len(q_losses) * self.q_metric.sign
            ddr_loss = torch.tensor([quantile_metric], dtype=torch.float32)
            losses_dict["ddr"] = ddr_loss
        return losses_dict


__all__ = ["DDR"]
