import os
import torch
import optuna

import numpy as np

from typing import *
from cfdata.tabular import DataLoader
from cfdata.tabular import TabularData
from cfdata.tabular import TabularDataset
from cftool.ml import Tracker
from cftool.ml import ModelPattern
from cftool.misc import shallow_copy_dict
from cftool.misc import lock_manager
from cftool.misc import timing_context
from cftool.misc import Saving
from cftool.misc import LoggingMixin
from trains import Task
from trains import Logger

try:
    amp: Optional[Any] = torch.cuda.amp
except:
    amp = None

from .inference import PreProcessor
from ..misc.toolkit import to_2d
from ..misc.time_series import TSLabelCollator
from ..models.base import model_dict
from ..pipeline.inference import Inference
from ..trainer.core import Trainer
from ..types import data_type

trains_logger: Optional[Logger] = None


class Pipeline(LoggingMixin):
    def __init__(
        self,
        config: Dict[str, Any],
        *,
        trial: optuna.trial.Trial = None,
        tracker_config: Dict[str, Any] = None,
        cuda: Union[str, int] = None,
        verbose_level: int = 2,
    ):
        self.trial = trial
        self.inference: Optional[Inference]
        self.tracker = None if tracker_config is None else Tracker(**tracker_config)
        self._verbose_level = int(verbose_level)
        if cuda == "cpu":
            self.device = torch.device("cpu")
        elif cuda is not None:
            self.device = torch.device(f"cuda:{cuda}")
        else:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._init_config(config)

    def __str__(self) -> str:
        return f"{type(self.model).__name__}()"  # type: ignore

    __repr__ = __str__

    @property
    def train_set(self) -> TabularDataset:
        raw = self.tr_data.raw
        return TabularDataset(*raw.xy, task_type=self.tr_data.task_type)

    @property
    def valid_set(self) -> Optional[TabularDataset]:
        if self.cv_data is None:
            return None
        raw = self.cv_data.raw
        return TabularDataset(*raw.xy, task_type=self.cv_data.task_type)

    @property
    def binary_threshold(self) -> Optional[float]:
        if self.inference is None:
            raise ValueError("`inference` is not yet generated")
        return self.inference.binary_threshold

    def _init_config(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.use_tqdm = self.config.setdefault("use_tqdm", True)
        self.timing = self.config.setdefault("use_timing_context", True)
        self._data_config = self.config.setdefault("data_config", {})
        self._data_config["use_timing_context"] = self.timing
        self._data_config["default_categorical_process"] = "identical"
        self._read_config = self.config.setdefault("read_config", {})
        self._cv_split = self.config.setdefault("cv_split", 0.1)
        self._cv_split_order = self.config.setdefault("cv_split_order", "auto")
        self._model = self.config.setdefault("model", "fcnn")
        self._is_binary = self.config.get("is_binary")
        self._binary_config = self.config.setdefault("binary_config", {})
        self._binary_config.setdefault("binary_metric", "acc")

        self.shuffle_tr = self.config.setdefault("shuffle_tr", True)
        self.batch_size = self.config.setdefault("batch_size", 128)
        default_cv_bz = 5 * self.batch_size
        self.cv_batch_size = self.config.setdefault("cv_batch_size", default_cv_bz)

        self._sampler_config = self.config.setdefault("sampler_config", {})
        self._ts_label_collator_config = self.config.setdefault(
            "ts_label_collator_config", {}
        )

        logging_folder = self.config["logging_folder"] = self.config.setdefault(
            "logging_folder",
            os.path.join("_logging", model_dict[self._model].__identifier__),
        )
        logging_file = self.config.get("logging_file")
        if logging_file is not None:
            logging_path = os.path.join(logging_folder, logging_file)
        else:
            logging_path = os.path.abspath(self.generate_logging_path(logging_folder))
        self.config["_logging_path_"] = logging_path
        trigger_logging = self.config.setdefault("trigger_logging", False)
        self._init_logging(self._verbose_level, trigger_logging)

    def _init_data(self) -> None:
        if not self.tr_data.is_ts:
            self.ts_label_collator = None
        else:
            self.ts_label_collator = TSLabelCollator(
                self.tr_data,
                self._ts_label_collator_config,
            )
        self._sampler_config.setdefault("verbose_level", self.tr_data._verbose_level)
        self.preprocessor = PreProcessor(self._original_data, self._sampler_config)
        tr_sampler = self.preprocessor.make_sampler(self.tr_data, self.shuffle_tr)
        self.tr_loader = DataLoader(
            self.batch_size,
            tr_sampler,
            return_indices=True,
            verbose_level=self._verbose_level,
            label_collator=self.ts_label_collator,
        )
        if self.cv_data is None:
            self.cv_loader = None
        else:
            cv_sampler = self.preprocessor.make_sampler(self.cv_data, False)
            self.cv_loader = DataLoader(
                self.cv_batch_size,
                cv_sampler,
                return_indices=True,
                verbose_level=self._verbose_level,
                label_collator=self.ts_label_collator,
            )

    def _prepare_modules(self, *, is_loading: bool = False) -> None:
        # model
        with timing_context(self, "init model", enable=self.timing):
            args = self.config, self.tr_data, self.cv_data, self.device
            self.model = model_dict[self._model](*args)
        # trainer
        with timing_context(self, "init trainer", enable=self.timing):
            if self.preprocessor is None:
                msg = "`preprocessor` is not defined. Please call `_init_data` first"
                raise ValueError(msg)
            self.inference = Inference(
                self.preprocessor,
                self.model.device,
                model=self.model,
                binary_config=self._binary_config,
                use_tqdm=self.use_tqdm,
            )
            self.trainer = Trainer(
                self.model,
                self.inference,
                self.trial,
                self.tracker,
                self.config,
                self._verbose_level,
                is_loading,
            )
        # to device
        with timing_context(self, "init device", enable=self.timing):
            self.trainer.model.to(self.device)

    # TODO : Call this frequently
    def _generate_binary_threshold(self) -> None:
        if self.inference is None:
            raise ValueError("`inference` is not yet generated")
        self.inference.generate_binary_threshold()

    def _before_loop(
        self,
        x: data_type,
        y: data_type,
        x_cv: data_type,
        y_cv: data_type,
        sample_weights: np.ndarray,
    ) -> None:
        # data
        y, y_cv = map(to_2d, [y, y_cv])
        args = (x, y) if y is not None else (x,)
        self._data_config["verbose_level"] = self._verbose_level
        self._original_data = TabularData(**self._data_config)
        self._original_data.read(*args, **self._read_config)
        self.tr_data = self._original_data
        self._save_original_data = False
        self.tr_weights = None
        if x_cv is not None:
            self.cv_data = self.tr_data.copy_to(x_cv, y_cv)
            if sample_weights is not None:
                self.tr_weights = sample_weights[: len(self.tr_data)]
        else:
            if self._cv_split <= 0.0:
                self.cv_data = self.cv_indices = None
                if sample_weights is not None:
                    self.tr_weights = sample_weights
            else:
                self._save_original_data = True
                split = self.tr_data.split(
                    self._cv_split,
                    order=self._cv_split_order,
                )
                self.cv_data, self.tr_data = split.split, split.remained
                # TODO : utilize cv_weights with sample_weights[split.split_indices]
                if sample_weights is not None:
                    self.tr_weights = sample_weights[split.remained_indices]
        self._init_data()
        # modules
        self._prepare_modules()

    def _loop(self) -> None:
        # training loop
        self.trainer.fit(self.tr_loader, self.cv_loader, self.tr_weights)
        # binary threshold
        self._generate_binary_threshold()
        # logging
        self.log_timing()

    # api

    def fit(
        self,
        x: data_type,
        y: data_type = None,
        x_cv: data_type = None,
        y_cv: data_type = None,
        *,
        sample_weights: np.ndarray = None,
    ) -> "Pipeline":
        self._before_loop(x, y, x_cv, y_cv, sample_weights)
        self._loop()
        return self

    def trains(
        self,
        x: data_type,
        y: data_type = None,
        x_cv: data_type = None,
        y_cv: data_type = None,
        *,
        sample_weights: np.ndarray = None,
        trains_config: Dict[str, Any] = None,
        keep_task_open: bool = False,
        queue: str = None,
    ) -> "Pipeline":
        if trains_config is None:
            return self.fit(x, y, x_cv, y_cv, sample_weights=sample_weights)
        # init trains
        if trains_config is None:
            trains_config = {}
        project_name = trains_config.get("project_name")
        task_name = trains_config.get("task_name")
        if queue is None:
            task = Task.init(**trains_config)
            cloned_task = None
        else:
            task = Task.get_task(project_name=project_name, task_name=task_name)
            cloned_task = Task.clone(source_task=task, parent=task.id)
        # before loop
        self._verbose_level = 6
        self._data_config["verbose_level"] = 6
        self._before_loop(x, y, x_cv, y_cv, sample_weights)
        self.trainer.use_tqdm = False
        copied_config = shallow_copy_dict(self.config)
        if queue is not None:
            assert cloned_task is not None
            cloned_task.set_parameters(copied_config)
            Task.enqueue(cloned_task.id, queue)
            return self
        # loop
        task.connect(copied_config)
        global trains_logger
        trains_logger = task.get_logger()
        self._loop()
        if not keep_task_open:
            task.close()
            trains_logger = None
        return self

    def predict(
        self,
        x: data_type,
        *,
        return_all: bool = False,
        contains_labels: bool = False,
        requires_recover: bool = True,
        returns_probabilities: bool = False,
        **kwargs: Any,
    ) -> Union[np.ndarray, Dict[str, np.ndarray]]:
        loader = self.preprocessor.make_inference_loader(
            x,
            self.cv_batch_size,
            contains_labels=contains_labels,
        )
        kwargs.update(
            {
                "return_all": return_all,
                "requires_recover": requires_recover,
                "returns_probabilities": returns_probabilities,
            }
        )

        if self.inference is None:
            raise ValueError("`inference` is not yet generated")
        return self.inference.predict(loader, **kwargs)

    def predict_prob(
        self,
        x: data_type,
        *,
        return_all: bool = False,
        contains_labels: bool = False,
        **kwargs: Any,
    ) -> Union[np.ndarray, Dict[str, np.ndarray]]:
        if self.tr_data.is_reg:
            raise ValueError("`predict_prob` should not be called on regression tasks")
        return self.predict(
            x,
            return_all=return_all,
            contains_labels=contains_labels,
            returns_probabilities=True,
            **kwargs,
        )

    def to_pattern(
        self,
        *,
        pre_process: Callable = None,
        **predict_kwargs: Any,
    ) -> ModelPattern:
        def _predict(x: np.ndarray) -> np.ndarray:
            if pre_process is not None:
                x = pre_process(x)
            return self.predict(x, **predict_kwargs)

        def _predict_prob(x: np.ndarray) -> np.ndarray:
            if pre_process is not None:
                x = pre_process(x)
            return self.predict_prob(x, **predict_kwargs)

        return ModelPattern(
            init_method=lambda: self,
            predict_method=_predict,
            predict_prob_method=_predict_prob,
        )

    def save(self, export_folder: str = None, *, compress: bool = True) -> "Pipeline":
        if export_folder is None:
            export_folder = self.trainer.checkpoint_folder
        abs_folder = os.path.abspath(export_folder)
        base_folder = os.path.dirname(abs_folder)
        with lock_manager(base_folder, [export_folder]):
            Saving.prepare_folder(self, export_folder)
            # TODO : save indices instead of instance. Only save original data
            train_data_folder = os.path.join(export_folder, "__data__", "train")
            if self._save_original_data:
                self._original_data.save(train_data_folder, compress=compress)
            else:
                self.tr_data.save(train_data_folder, compress=compress)
                valid_data_folder = os.path.join(export_folder, "__data__", "valid")
                if self.cv_data is not None:
                    self.cv_data.save(valid_data_folder, compress=compress)
            self.trainer.save_checkpoint(export_folder)
            self.config["is_binary"] = self._is_binary
            if self.inference is None:
                raise ValueError("`inference` is not yet generated")
            self.config["binary_config"] = self.inference.binary_config
            Saving.save_dict(self.config, "config", export_folder)
            if compress:
                Saving.compress(abs_folder, remove_original=True)
        return self

    @classmethod
    def load(
        cls,
        folder: str,
        *,
        cuda: int = None,
        verbose_level: int = 0,
        compress: bool = True,
    ) -> "Pipeline":
        base_folder = os.path.dirname(os.path.abspath(folder))
        with lock_manager(base_folder, [folder]):
            with Saving.compress_loader(folder, compress, remove_extracted=True):
                config = Saving.load_dict("config", folder)
                pipeline = Pipeline(config, cuda=cuda, verbose_level=verbose_level)
                tr_data_folder = os.path.join(folder, "__data__", "train")
                cv_data_folder = os.path.join(folder, "__data__", "valid")
                tr_data = TabularData.load(tr_data_folder, compress=compress)
                cv_data = None
                cv_file = f"{cv_data_folder}.zip"
                if os.path.isdir(cv_data_folder) or os.path.isfile(cv_file):
                    cv_data = TabularData.load(cv_data_folder, compress=compress)
                pipeline._original_data = tr_data
                pipeline.tr_data = tr_data
                pipeline.cv_data = cv_data
                pipeline._init_data()
                pipeline._prepare_modules(is_loading=True)
                trainer = pipeline.trainer
                trainer.tr_loader = pipeline.tr_loader
                trainer.cv_loader = pipeline.cv_loader
                trainer.restore_checkpoint(folder)
                trainer._init_metrics()
        return pipeline


__all__ = ["Pipeline"]
