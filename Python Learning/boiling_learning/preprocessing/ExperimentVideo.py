from contextlib import contextmanager
import dataclasses
from dataclasses import dataclass
import operator
from pathlib import Path
from typing import (
    overload,
    Any,
    Callable,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union
)

# import decord
import funcy
import numpy as np
import modin.pandas as pd
import pims
from scipy.interpolate import interp1d
import tensorflow as tf

import boiling_learning.utils as bl_utils
from boiling_learning.utils import (
    PathType,
    VerboseType
)
from boiling_learning.io.io import (
    chunked_filename_pattern
)
from boiling_learning.preprocessing.preprocessing import (
    sync_dataframes
)
from boiling_learning.preprocessing.video import (
    convert_video,
    extract_audio,
    extract_frames,
    frames
)


class ExperimentVideo:
    @dataclass
    class VideoData:
        '''Class for video data representation.
        # TODO: improve this doc

        Attributes
        ----------
        categories: [...]. Example: {
                'wire': 'NI80-...',
                'nominal_power': 85
            }
        fps: [...]. Example: 30
        ref_image: [...]. Example: 'GOPR_frame1263.png'
        ref_elapsed_time: [...]. Example: 12103
        '''
        categories: Optional[Mapping[str, Any]] = dataclasses.field(default_factory=dict)
        fps: Optional[float] = None
        ref_index: Optional[str] = None
        ref_elapsed_time: Optional[str] = None

    @dataclass(frozen=True)
    class VideoDataKeys:
        categories: str = 'categories'
        fps: str = 'fps'
        ref_index: str = 'ref_index'
        ref_elapsed_time: str = 'ref_elapsed_time'

    @dataclass(frozen=True)
    class DataFrameColumnNames:
        index: str = 'index'
        path: Optional[str] = None
        name: str = 'name'
        elapsed_time: str = 'elapsed_time'

    @dataclass(frozen=True)
    class DataFrameColumnTypes:
        index = int
        path = str
        name = str
        elapsed_time = 'timedelta64[s]'
        categories = 'category'

    def __init__(
            self,
            video_path: PathType,
            name: Optional[str] = None,
            frames_dir: Optional[PathType] = None,
            frames_suffix: str = '.png',
            frames_path: Optional[PathType] = None,
            audio_dir: Optional[PathType] = None,
            audio_suffix: str = '.m4a',
            audio_path: Optional[PathType] = None,
            df_dir: Optional[PathType] = None,
            df_suffix: str = '.csv',
            df_path: Optional[PathType] = None,
            column_names: DataFrameColumnNames = DataFrameColumnNames(),
            column_types: DataFrameColumnTypes = DataFrameColumnTypes()
    ):
        self.video_path: Path = bl_utils.ensure_resolved(video_path)
        self.frames_path: Path
        self.frames_suffix: str
        self.audio_path: Path
        self.df_path: Path
        self.data: Optional[self.VideoData] = None
        self._name: str
        self.column_names: self.DataFrameColumnNames = column_names
        self.column_types: self.DataFrameColumnTypes = column_types
        self.df: Optional[pd.DataFrame] = None
        self.ds: Optional[tf.data.Dataset] = None
        # self.video: Optional[decord.VideoReader] = None
        self.video: Optional[pims.Video] = None
        self._is_open_video: bool = False

        if name is None:
            self._name = self.video_path.stem
        else:
            self._name = name

        if (frames_dir is None) ^ (frames_path is None):
            self.frames_path = (
                bl_utils.ensure_resolved(frames_path)
                if frames_dir is None
                else bl_utils.ensure_resolved(frames_dir) / self.name
            )
        else:
            raise ValueError(
                'exactly one of (frames_dir, frames_path) must be given.')

        if frames_suffix.startswith('.'):
            self.frames_suffix = frames_suffix
        else:
            raise ValueError(
                'argument *frames_suffix* must start with a dot \'.\'')

        if not audio_suffix.startswith('.'):
            raise ValueError(
                'argument *audio_suffix* must start with a dot \'.\'')

        if (audio_dir is None) ^ (audio_path is None):
            self.audio_path = (
                bl_utils.ensure_resolved(audio_path)
                if audio_dir is None
                else (bl_utils.ensure_resolved(audio_dir) / self.name).with_suffix(audio_suffix)
            )
        else:
            raise ValueError(
                'exactly one of (audio_dir, audio_path) must be given.')

        if not df_suffix.startswith('.'):
            raise ValueError(
                'argument *df_suffix* must start with a dot \'.\'')

        if (df_dir is None) ^ (df_path is None):
            self.df_path = (
                bl_utils.ensure_resolved(df_path)
                if df_dir is None
                else (bl_utils.ensure_resolved(df_dir) / self.name).with_suffix(df_suffix)
            )
        else:
            raise ValueError(
                'exactly one of (df_dir, df_path) must be given.')

    def __str__(self) -> str:
        return ''.join((
            self.__class__.__name__,
            '(',
            ', '.join((
                f'name={self.name}',
                f'video_path={self.video_path}',
                f'frames_path={self.frames_path}',
                f'frames_suffix={self.frames_suffix}',
                f'audio_path={self.audio_path}',
                f'df_path={self.df_path}',
                f'data={self.data}',
                f'column_names={self.column_names}',
                f'column_types={self.column_types}',
            )),
            ')'
        ))

    @property
    def name(self) -> str:
        return self._name

    def open_video(self) -> None:
        # decord.bridge.set_bridge('tensorflow')
        if not self._is_open_video:
            self.video = pims.Video(str(self.video_path))
            # self.video = decord.VideoReader(str(self.video_path))
            self._is_open_video = True

    def close_video(self) -> None:
        if self._is_open_video:
            self.video.close()
        self.video = None
        self._is_open_video = False

    def convert_video(
            self,
            dest_path: PathType,
            overwrite: bool = False,
            verbose: VerboseType = False
    ) -> None:
        """Use this function to move or convert video
        """
        dest_path = bl_utils.ensure_parent(dest_path)
        convert_video(
            self.video_path,
            dest_path,
            overwrite=overwrite,
            verbose=verbose
        )
        self.video_path = dest_path

    def extract_audio(
            self,
            overwrite: bool = False,
            verbose: VerboseType = False
    ) -> None:
        extract_audio(
            self.video_path,
            self.audio_path,
            overwrite=overwrite,
            verbose=verbose
        )

    def extract_frames(
            self,
            chunk_sizes: Optional[List[int]] = None,
            prepend_name: bool = True,
            iterate: bool = True,
            overwrite: bool = False,
            verbose: VerboseType = False
    ) -> None:
        filename_pattern = 'frame{index}' + self.frames_suffix
        if prepend_name:
            filename_pattern = '_'.join((self.name, filename_pattern))

        if chunk_sizes is not None:
            filename_pattern = chunked_filename_pattern(
                chunk_sizes=chunk_sizes,
                chunk_name='{min_index}-{max_index}',
                filename=filename_pattern
            )

        extract_frames(
            self.video_path,
            outputdir=self.frames_path,
            filename_pattern=filename_pattern,
            frame_suffix=self.frames_suffix,
            verbose=verbose,
            fast_frames_count=None if overwrite else not iterate,
            overwrite=overwrite,
            iterate=iterate
        )

    def _frame_stem_format(self) -> str:
        return self.name + '_frame{index}'

    def frame_stem(self, index: int) -> str:
        return self._frame_stem_format().format(index=index)

    def _frame_name_format(self) -> str:
        return self._frame_stem_format() + self.frames_suffix

    def frame_name(self, index: int) -> str:
        return self._frame_name_format().format(index=index)

    @contextmanager
    def sequential_frames(self) -> Iterator[Iterable[np.ndarray]]:
        f = frames(self.video_path)
        try:
            yield f
        finally:
            f.close()

    @contextmanager
    def frames(self, auto_open: bool = True) -> Iterator[Optional[Sequence[np.ndarray]]]:
        if auto_open:
            self.open_video()
        yield self.video

    # @contextmanager
    # def frames(self) -> Iterator[Sequence[np.ndarray]]:
    #     f = pims.Video(self.video_path)
    #     try:
    #         yield f
    #     finally:
    #         f.close()

    def frame(self, i: int, auto_open: bool = True) -> np.ndarray:
        if auto_open:
            self.open_video()
        elif not self._is_open_video:
            raise ValueError('Video is not open. Please *open_video()* before getting frame.')
        return self.video[i]
        # with self.frames() as f:
        #     return f[i]

    def glob_frames(self) -> Iterable[Path]:
        return self.frames_path.rglob('*' + self.frames_suffix)

    def set_video_data(
            self,
            data: Union[Mapping[str, Any], VideoData],
            keys: VideoDataKeys = VideoDataKeys()
    ) -> None:
        if isinstance(data, self.VideoData):
            self.data = data
        else:
            self.data = bl_utils.dataclass_from_mapping(
                data,
                self.VideoData,
                key_map=keys
            )

    def set_data(
            self,
            data_source: pd.DataFrame,
            source_time_column: str
    ) -> pd.DataFrame:
        '''Define data (other than the ones specified as video data) from a source *data_source*

        Example usage:
        >>>> data_source = pd.read_csv('my_data.csv')
        >>>> time_column, hf_column, temperature_column = 'time', 'heat_flux', 'temperature'
        >>>> ev.set_data(
            data_source[[time_column, hf_column, temperature_column]],
            source_time_column=time_column
        )

        WARNING: if *data_source* contains
        '''
        self.make_dataframe(recalculate=False, enforce_time=True)

        columns_to_set = tuple(x for x in data_source.columns if x != source_time_column)
        intersect = frozenset(columns_to_set) & frozenset(self.df.columns)
        if intersect:
            raise ValueError(
                f'the columns {intersect} exist both in *data_source* and in this dataframe.'
                ' Make sure you rename *data_source* columns to avoid this error.')

        time = data_source[source_time_column]
        for column in columns_to_set:
            interpolator = interp1d(time, data_source[column])
            self.df[column] = interpolator(self.df[self.column_names.elapsed_time])

        return self.df

    def convert_dataframe_type(self, df: pd.DataFrame, categories_as_int: bool = False) -> pd.DataFrame:
        col_types = funcy.merge(
            dict.fromkeys(self.data.categories, 'category'),
            {
                self.column_names.index: self.column_types.index,
                self.column_names.path: self.column_types.path,
                self.column_names.name: self.column_types.name,
                # self.column_names.elapsed_time: self.column_types.elapsed_time
                # BUG: including the line above rounds elapsed time, breaking the whole pipeline
            }
        )
        col_types = funcy.select_keys(
            set(df.columns),
            col_types
        )
        df = df.astype(col_types)

        if df[self.column_names.elapsed_time].dtype.kind == 'm':
            df[self.column_names.elapsed_time] = df[self.column_names.elapsed_time].dt.total_seconds()

        if categories_as_int:
            df = bl_utils.dataframe_categories_to_int(df, inplace=True)

        return df

    def make_dataframe(
            self,
            recalculate: bool = False,
            exist_load: bool = False,
            enforce_time: bool = False,
            categories_as_int: bool = False,
            inplace: bool = True
    ) -> pd.DataFrame:
        if not recalculate and self.df is not None:
            return self.df

        if exist_load and self.df_path.is_file():
            self.load_df()
            return self.df

        if self.data is None:
            raise ValueError(
                'cannot convert to DataFrame. Video data must be previously set.')

        with self.frames() as f:
            indices = range(len(f))

        data = bl_utils.merge_dicts(
            {
                self.column_names.name: self.name,
                self.column_names.index: list(indices)
            },
            self.data.categories,
            latter_precedence=False
        )

        available_time_info = map(
            bl_utils.is_not(None),
            (
                self.data.fps,
                self.data.ref_index,
                self.data.ref_elapsed_time
            )
        )
        if all(available_time_info):
            ref_index = self.data.ref_index
            ref_elapsed_time = pd.to_timedelta(self.data.ref_elapsed_time, unit='s')
            delta = pd.to_timedelta(1/self.data.fps, unit='s')
            elapsed_time_list = [
                ref_elapsed_time + delta*(index - ref_index)
                for index in indices
            ]

            data[self.column_names.elapsed_time] = elapsed_time_list
        elif enforce_time:
            raise ValueError(
                'there is not enough time info in video data'
                ' (set *enforce_time*=False to suppress this error).')

        if self.column_names.path is not None:
            paths = sorted(
                self.glob_frames(),
                key=operator.attrgetter('stem')
            )
            data[self.column_names.path] = paths

        df = pd.DataFrame(data)
        df = self.convert_dataframe_type(
            df,
            categories_as_int=categories_as_int
        )

        if inplace:
            self.df = df
        return df

    def sync_time_series(
            self,
            source_df: pd.DataFrame,
            inplace: bool = True
    ) -> pd.DataFrame:
        df = self.make_dataframe(recalculate=False, enforce_time=True, inplace=inplace)

        df = sync_dataframes(
            source_df=source_df,
            dest_df=df,
            dest_time_column=self.column_names.elapsed_time
        )

        if inplace:
            self.df = df

        return df

    @overload
    def iterdata_from_dataframe(self, select_columns: str) -> Iterable[Tuple[np.ndarray, Any]]: ...

    @overload
    def iterdata_from_dataframe(self, select_columns: Optional[List[str]]) -> Iterable[Tuple[np.ndarray, dict]]: ...

    def iterdata_from_dataframe(self, select_columns=None):
        df = self.make_dataframe(recalculate=False)
        indices = df[self.column_names.index]

        data = df
        if select_columns is not None:
            data = data[select_columns]
            if not isinstance(select_columns, str):
                data = data.to_dict(orient='records')

        return zip(
            map(self.frame, indices),
            data
        )

    def load_df(
            self,
            path: Optional[PathType] = None,
            columns: Optional[Iterable[str]] = None,
            overwrite: bool = False,
            missing_ok: bool = False,
            inplace: bool = True
    ) -> Optional[pd.DataFrame]:
        if not overwrite and self.df is not None:
            return self.df

        if path is None:
            path = self.df_path
        else:
            self.df_path = bl_utils.ensure_resolved(path)

        if missing_ok and not self.df_path.is_file():
            return None

        if columns is None:
            df = pd.read_csv(self.df_path, skipinitialspace=True)
        else:
            df = pd.read_csv(
                self.df_path,
                skipinitialspace=True,
                usecols=tuple(columns)
            )

        if inplace:
            self.df = df
        return df

    def save_df(
            self,
            path: Optional[PathType] = None,
            overwrite: bool = False
    ) -> None:
        if path is None:
            path = self.df_path
        path = bl_utils.ensure_parent(path)

        if overwrite or not path.is_file():
            self.df.to_csv(path, index=False)

    def move_df(
            self,
            path: Union[str, bl_utils.PathType],
            renaming: bool = False,
            erase_old: bool = False,
            overwrite: bool = False
    ) -> None:
        if erase_old:
            old_path = self.df_path

        if renaming:
            self.df_path = self.df_path.with_name(path)
        else:
            self.df_path = bl_utils.ensure_resolved(path)

        self.save(overwrite=overwrite)

        if erase_old and old_path.is_file():
            old_path.unlink()
        # if erase: # Python 3.8 only
        #     old_path.unlink(missing_ok=True)

    def as_tf_dataset(
            self,
            select_columns: Optional[Union[str, List[str]]] = None,
            sequential_indices_call: Optional[Callable[['ExperimentVideo'], tf.data.Dataset]] = None,
            inplace: bool = False
    ) -> tf.data.Dataset:
        # See <https://www.tensorflow.org/tutorials/load_data/pandas_dataframe>

        df = self.make_dataframe(recalculate=False)
        df = self.convert_dataframe_type(df)
        indices = df[self.column_names.index]

        if sequential_indices_call is not None and bl_utils.is_consecutive(indices):
            ds_img = sequential_indices_call(self)
        else:
            def remapped_frames(indices: Iterable[int]) -> Iterable[tf.Tensor]:
                frames = map(self.frame, indices)
                # tf_frames = map(decord.bridge.to_tensorflow, frames)
                converter = funcy.rpartial(tf.image.convert_image_dtype, tf.float32)
                # return map(converter, tf_frames)
                return map(converter, frames)

            ds_img = tf.data.Dataset.from_generator(
                remapped_frames,
                tf.float32,
                args=[indices]
            )

        if select_columns is not None:
            df = df[select_columns]

        ds_data = tf.data.Dataset.from_tensor_slices(
            df.to_dict('list')
        )
        ds = tf.data.Dataset.zip((ds_img, ds_data))

        if inplace:
            self.ds = ds

        return ds

        # return tf.data.Dataset.from_generator(
        #     self.iterdata_from_dataframe,
        #     (tf.float32, type_spec),
        #     args=[select_columns]
        # )