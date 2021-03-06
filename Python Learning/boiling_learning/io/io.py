import collections
from functools import partial
from itertools import accumulate
import io as _io
import json
import operator
import os
from pathlib import Path
import pickle
import string
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union
)
import warnings

import cv2
import funcy
import h5py
import json_tricks
import numpy as np
import modin.pandas as pd
import tensorflow as tf
from tensorflow.keras.models import load_model
try:
    # yogadl is an optional dependency
    # TODO: read <https://stackoverflow.com/a/27361558/5811400>
    import yogadl
    import yogadl.storage
except ImportError:
    pass # TODO: handle this case

import boiling_learning.utils as bl_utils
from boiling_learning.utils.functional import pack
from boiling_learning.utils import (
    PathType,
    ensure_dir,
    ensure_parent,
    ensure_resolved
)

T = TypeVar('T')
S = TypeVar('S')
SaverFunction = Callable[[S, PathType], Any]
LoaderFunction = Callable[[PathType], S]
DatasetTriplet = Tuple[tf.data.Dataset, Optional[tf.data.Dataset], tf.data.Dataset]
OptionalDatasetTriplet = Tuple[Optional[tf.data.Dataset], Optional[tf.data.Dataset], Optional[tf.data.Dataset]]
BoolFlagged = Tuple[bool, S]
BoolFlaggedLoaderFunction = LoaderFunction[BoolFlagged[S]]


def add_bool_flag(
    loader: LoaderFunction[T],
    expected_exceptions: Union[Exception, Sequence[Exception]] = FileNotFoundError
) -> BoolFlaggedLoaderFunction[T]:
    if isinstance(expected_exceptions, Sequence):
        expected_exceptions = tuple(expected_exceptions)

    def _loader(path: PathType) -> T:
        try:
            return True, loader(path)
        except expected_exceptions:
            return False, None

    return _loader


def chunked_filename_pattern(
        chunk_sizes: Iterable[int],
        chunk_name: str = '{min_index}-{max_index}',
        filename: PathType = 'frame{index}.png',
        index_key: str = 'index',
        min_index_key: str = 'min_index',
        max_index_key: str = 'max_index',
        root: Optional[PathType] = None
) -> Callable[[int], Path]:
    chunks = tuple(accumulate(chunk_sizes, operator.mul))
    filename_formatter = filename.format
    chunk_name_formatter = chunk_name.format

    def filename_pattern(index: int) -> Path:
        current = Path(filename_formatter(**{index_key: index}))
        for chunk_size in chunks:
            min_index = (index // chunk_size) * chunk_size
            max_index = min_index + chunk_size - 1
            current_chunk_name = chunk_name_formatter(
                **{min_index_key: min_index, max_index_key: max_index})
            current = Path(current_chunk_name) / current

        current = bl_utils.ensure_resolved(current, root=root)

        return current
    return filename_pattern


def make_callable_filename_pattern(
        outputdir: PathType,
        filename_pattern: Union[PathType, Callable[[int], PathType]],
        index_key: Optional[str] = None
) -> Tuple[bool, Callable[[int], Path]]:

    if callable(filename_pattern):
        def _filename_pattern(index: int) -> Path:
            return bl_utils.ensure_parent(
                filename_pattern(index),
                root=outputdir
            )

        return True, _filename_pattern
    else:
        filename_pattern_str = str(filename_pattern)

        if index_key is not None and index_key in {
                tup[1]
                for tup in string.Formatter().parse(filename_pattern_str)
                if tup[1] is not None
        }:
            formatter = filename_pattern_str.format

            def _filename_pattern(index: int) -> Path:
                return bl_utils.ensure_parent(
                    formatter(
                        **{index_key: index}
                    ),
                    root=outputdir
                )
            return True, _filename_pattern

        else:
            try:
                # checks if it is possible to use old-style formatting
                filename_pattern_str % 0
            except TypeError:
                return False, filename_pattern

            def _filename_pattern(index: int) -> Path:
                return bl_utils.ensure_parent(
                    filename_pattern_str % index,
                    root=outputdir
                )

            return True, _filename_pattern


def save_image(
        image: np.ndarray,
        path: PathType
) -> None:
    cv2.imwrite(
        str(bl_utils.ensure_parent(path)),
        image
    )


def load_image(path: PathType, flag: Optional[int] = cv2.IMREAD_COLOR) -> np.ndarray:
    return cv2.imread(
        str(bl_utils.ensure_resolved(path)),
        flag
    )


def save_serialized(
        save_map: Mapping[T, SaverFunction[S]]
) -> SaverFunction[Mapping[T, S]]:
    def save(return_dict: Mapping[T, S], path: PathType) -> None:
        path = ensure_parent(path)
        for key, obj in return_dict.items():
            save_map[key](obj, path / key)
    return save


def load_serialized(
        load_map: Mapping[T, LoaderFunction[S]]
) -> LoaderFunction[Dict[T, S]]:
    def load(path: PathType) -> Dict[T, S]:
        path = ensure_resolved(path)
        loaded = {
            key: loader(path / key)
            for key, loader in load_map.items()
        }
        return loaded
    return load


def save_keras_model(keras_model, path: PathType, **kwargs) -> None:
    path = ensure_parent(path)
    keras_model.save(path, **kwargs)


def load_keras_model(path: PathType, strategy=None, **kwargs):
    if strategy is None:
        scope = funcy.nullcontext()
    else:
        scope = strategy.scope()

    with scope:
        return load_model(path, **kwargs)


def save_pkl(obj, path: PathType) -> None:
    path = ensure_parent(path)

    with path.open('wb') as file:
        pickle.dump(obj, file, protocol=pickle.HIGHEST_PROTOCOL)


def load_pkl(path: PathType):
    with ensure_resolved(path).open('rb') as file:
        return pickle.load(file)


def save_json(
        obj: T,
        path: PathType,
        dump: Callable[[T, _io.TextIOWrapper], Any] = json.dump,
        cls: Optional[Type] = None
) -> None:
    path = ensure_parent(path)

    if path.suffix != '.json':
        warnings.warn(
            f'A JSON file is expected, but *path* ends with "{path.suffix}"',
            category=RuntimeWarning
        )

    dump = pack(cls=cls).omit('cls', bl_utils.is_(None)).partial(dump)
    with path.open('w', encoding='utf-8') as file:
        dump(obj, file, indent=4, ensure_ascii=False)


def load_json(
        path: PathType,
        load: Callable[[_io.TextIOWrapper], T] = json.load,
        cls: Optional[Type] = None
) -> T:
    path = ensure_resolved(path)

    if path.suffix != '.json':
        warnings.warn(
            f'A JSON file is expected, but *path* ends with "{path.suffix}"',
            category=RuntimeWarning
        )

    load = pack(cls=cls).omit('cls', bl_utils.is_(None)).partial(load)
    with path.open('r', encoding='utf-8') as file:
        return load(file)


def saver_hdf5(key: str = '') -> SaverFunction[Any]:
    def save_hdf5(obj, path: PathType) -> None:
        path = ensure_parent(path)
        with h5py.File(str(path), 'w') as hf:
            hf.create_dataset(key, data=obj)
    return save_hdf5


def loader_hdf5(key: str = '') -> LoaderFunction[Any]:
    def load_hdf5(path: PathType):
        path = ensure_resolved(path)
        with h5py.File(str(path), 'r') as hf:
            return hf.get(key)
    return load_hdf5


def save_element_spec(element_spec: tf.TensorSpec, path: PathType) -> None:
    encoded_element_spec = bl_utils.dtypes.encode_element_spec(element_spec)
    save_json(encoded_element_spec, path, dump=json_tricks.dump)


def load_element_spec(path: PathType) -> tf.TensorSpec:
    encoded_element_spec = load_json(path, load=json_tricks.load)
    return bl_utils.dtypes.decode_element_spec(encoded_element_spec)


def save_dataset(dataset: tf.data.Dataset, path: PathType) -> None:
    path = ensure_dir(path)
    dataset_path = path / 'dataset.tensorflow'
    element_spec_path = path / 'element_spec.json'

    save_element_spec(dataset.element_spec, element_spec_path)
    tf.data.experimental.save(dataset, str(dataset_path))


def load_dataset(path: PathType) -> tf.data.Dataset:
    path = ensure_resolved(path)
    dataset_path = path / 'dataset.tensorflow'
    element_spec_path = path / 'element_spec.json'

    element_spec = load_element_spec(element_spec_path)

    def recurse_fix(elem_spec):
        if isinstance(elem_spec, list):
            return tuple(map(recurse_fix, elem_spec))
        elif isinstance(elem_spec, collections.OrderedDict):
            return dict(funcy.walk_values(recurse_fix, elem_spec))
        else:
            return elem_spec

    element_spec = recurse_fix(element_spec)

    return tf.data.experimental.load(str(dataset_path), element_spec)


def _default_filename_pattern(name: str, index: int) -> Path:
    return Path(name + '_' + index + '.png')


def save_frames_dataset(
        dataset: tf.data.Dataset,
        path: PathType,
        filename_pattern: Callable[[str, int], Path] = _default_filename_pattern,
        name_column: str = 'name',
        index_column: str = 'index'
) -> None:
    path = bl_utils.ensure_dir(path)
    imgs_path = bl_utils.ensure_dir(path / 'images')
    df_path = path / 'dataframe.csv'

    def _get_path(data):
        name = data[name_column].decode("utf-8")
        index = int(data[index_column])

        return imgs_path / filename_pattern(name, index)

    def _make_series(path, data):
        return pd.Series(data, name=path)

    df = pd.DataFrame()
    for img, data in dataset.as_numpy_iterator():
        img_path = _get_path(data)
        df = df.append(_make_series(img_path, data))
        save_image(img, img_path)

    df.to_csv(df_path, header=True, index=True)


def saver_frames_dataset(
        filename_pattern: Callable[[str, int], Path] = _default_filename_pattern,
        chunk_sizes: Optional[Sequence[int]] = (100, 100)
) -> SaverFunction[DatasetTriplet]:
    def _saver(
            ds: DatasetTriplet,
            path: PathType
    ) -> None:
        path = ensure_parent(path)
        ds_train, ds_val, ds_test = ds

        save_frames_dataset(
            ds_train,
            path / 'train',
            filename_pattern=partial(filename_pattern, chunk_sizes=chunk_sizes)
        )
        if ds_val is not None:
            save_frames_dataset(
                ds_val,
                path / 'val',
                filename_pattern=partial(filename_pattern, chunk_sizes=chunk_sizes)
            )
        save_frames_dataset(
            ds_test,
            path / 'test',
            filename_pattern=partial(filename_pattern, chunk_sizes=chunk_sizes)
        )

    return _saver


def decode_img(img, channels: int = 1):
    # convert the compressed string to a 3D uint8 tensor
    img = tf.image.decode_png(img, channels=channels)
    # Use `convert_image_dtype` to convert to floats in the [0,1] range
    img = tf.image.convert_image_dtype(img, tf.float32)
    # resize the image to the desired size
    # img = tf.image.resize(img, IMG_SHAPE[:2])
    # img = tf.reshape(img, IMG_SHAPE)
    return img


def process_path(
        file_path,
        in_dir: Optional[PathType] = None
):
    # from relative to absolute path
    if in_dir is not None:
        file_path = str(in_dir) + os.sep + file_path

    # load the raw data from the file as a string
    img = tf.io.read_file(file_path)
    # decode data
    img = decode_img(img)
    return img


def load_frames_dataset(
        path: PathType,
        shuffle: bool = True
) -> tf.data.Dataset:
    path = bl_utils.ensure_resolved(path)
    df_path = path / 'dataframe.csv'
    # element_spec_path = path / 'elem_spec.json'

    df = pd.read_csv(df_path, index_col=0)
    if shuffle:
        df = df.sample(frac=1)
    files = [
        str(bl_utils.ensure_resolved(path))
        for path in df.index
    ]
    df = df.reset_index(drop=True)

    ds_img = tf.data.Dataset.from_tensor_slices(files)
    ds_img = ds_img.map(process_path)
    ds_data = tf.data.Dataset.from_tensor_slices(df.to_dict('list'))
    ds = tf.data.Dataset.zip((ds_img, ds_data))

    return ds


def loader_frames_dataset(
        path: PathType
) -> Tuple[bool, Optional[tf.data.Dataset]]:
    path = bl_utils.ensure_resolved(path)

    try:
        return True, load_frames_dataset(path, shuffle=True)
    except FileNotFoundError:
        return False, None


def saver_dataset_triplet(
        saver: SaverFunction[tf.data.Dataset]
) -> SaverFunction[DatasetTriplet]:
    def _saver(
            ds: DatasetTriplet,
            path: PathType
    ) -> None:
        ds_train, ds_val, ds_test = ds

        path = ensure_dir(path)
        saver(ds_train, path / 'train')
        if ds_val is not None:
            saver(ds_val, path / 'val')
        saver(ds_test, path / 'test')

    return _saver


def loader_dataset_triplet(
        loader: BoolFlaggedLoaderFunction[Optional[tf.data.Dataset]]
) -> BoolFlaggedLoaderFunction[OptionalDatasetTriplet]:
    def _loader(
            path: PathType
    ) -> BoolFlagged[OptionalDatasetTriplet]:
        path = bl_utils.ensure_resolved(path)

        success_train, ds_train = loader(path / 'train')
        success_val, ds_val = loader(path / 'val')
        success_test, ds_test = loader(path / 'test')

        success = success_train and success_test
        if not success_val:
            ds_val = None

        return success, (ds_train, ds_val, ds_test)

    return _loader


def save_yogadl(
        dataset,
        storage_path: PathType,
        dataset_id: str,
        dataset_version: str = '0.0'
) -> None:
    storage_path = ensure_dir(storage_path)

    lfs_config = yogadl.storage.LFSConfigurations(str(storage_path))
    storage = yogadl.storage.LFSStorage(lfs_config)
    storage.submit(dataset, dataset_id, dataset_version)


def saver_yogadl(
        storage_path: PathType,
        dataset_id: str
) -> SaverFunction[DatasetTriplet]:
    storage_path = bl_utils.ensure_resolved(storage_path)
    id_train = dataset_id + '_train'
    id_val = dataset_id + '_val'
    id_test = dataset_id + '_test'

    def _saver(
            ds: DatasetTriplet,
            path: Optional[PathType] = None
    ) -> None:
        ds_train, ds_val, ds_test = ds

        save_yogadl(
            ds_train,
            storage_path=storage_path,
            dataset_id=id_train
        )
        if ds_val is not None:
            save_yogadl(
                ds_val,
                storage_path=storage_path,
                dataset_id=id_val
            )
        save_yogadl(
            ds_test,
            storage_path=storage_path,
            dataset_id=id_test
        )

    return _saver


def load_yogadl(
        storage_path: PathType,
        dataset_id: str,
        dataset_version: str = '0.0',
        start_offset: int = 0,
        shuffle: bool = False,
        skip_shuffle_at_epoch_end: bool = False,
        shuffle_seed: Optional[int] = None,
        shard_rank: int = 0,
        num_shards: int = 1,
        drop_shard_remainder: bool = False
) -> tf.data.Dataset:
    storage_path = ensure_resolved(storage_path)

    lfs_config = yogadl.storage.LFSConfigurations(str(storage_path))
    storage = yogadl.storage.LFSStorage(lfs_config)
    dataref = storage.fetch(dataset_id, dataset_version)
    stream = dataref.stream(
        start_offset=start_offset,
        shuffle=shuffle,
        skip_shuffle_at_epoch_end=skip_shuffle_at_epoch_end,
        shuffle_seed=shuffle_seed,
        shard_rank=shard_rank,
        num_shards=num_shards,
        drop_shard_remainder=drop_shard_remainder
    )
    return yogadl.tensorflow.make_tf_dataset(stream)


def loader_yogadl(
        storage_path: PathType,
        dataset_id: str
) -> LoaderFunction[DatasetTriplet]:
    storage_path = bl_utils.ensure_resolved(storage_path)
    id_train = dataset_id + '_train'
    id_val = dataset_id + '_val'
    id_test = dataset_id + '_test'

    def _loader(path: Optional[PathType] = None):
        try:
            ds_train = load_yogadl(
                storage_path=storage_path,
                dataset_id=id_train
            )
        except AssertionError:
            ds_train = None

        try:
            ds_val = load_yogadl(
                storage_path=storage_path,
                dataset_id=id_val
            )
        except AssertionError:
            ds_val = None

        try:
            ds_test = load_yogadl(
                storage_path=storage_path,
                dataset_id=id_test
            )
        except AssertionError:
            ds_test = None

        success = ds_train is not None and ds_test is not None
        return success, (ds_train, ds_val, ds_test)

    return _loader
