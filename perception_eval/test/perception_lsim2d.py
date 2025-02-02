# Copyright 2022 TIER IV, Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import logging
import tempfile
from typing import List
from typing import Union

from perception_eval.common.object2d import DynamicObject2D
from perception_eval.config import PerceptionEvaluationConfig
from perception_eval.evaluation import PerceptionFrameResult
from perception_eval.evaluation.metrics import MetricsScore
from perception_eval.evaluation.result.perception_frame_config import CriticalObjectFilterConfig
from perception_eval.evaluation.result.perception_frame_config import PerceptionPassFailConfig
from perception_eval.manager import PerceptionEvaluationManager
from perception_eval.tool import PerceptionAnalyzer2D
from perception_eval.util.debug import get_objects_with_difference2d
from perception_eval.util.logger_config import configure_logger


class PerceptionLSimMoc:
    def __init__(
        self,
        dataset_paths: List[str],
        evaluation_task: str,
        result_root_directory: str,
        label_prefix: str,
        camera_type: Union[str, List[str]],
    ):
        self.evaluation_task = evaluation_task
        self.label_prefix = label_prefix

        if evaluation_task in ("detection2d", "tracking2d"):
            evaluation_config_dict = {
                "evaluation_task": evaluation_task,
                "center_distance_thresholds": [
                    100,
                    200,
                ],  # = [[100, 100, 100, 100], [200, 200, 200, 200]]
                "iou_2d_thresholds": [0.5],  # = [[0.5, 0.5, 0.5, 0.5]]
            }
        elif evaluation_task == "classification2d":
            evaluation_config_dict = {"evaluation_task": evaluation_task}
        else:
            raise ValueError(f"Unexpected evaluation task: {evaluation_task}")

        # If target_labels = None, all labels will be evaluated.
        evaluation_config_dict.update(
            dict(
                target_labels=["green", "red", "yellow", "unknown"]
                if label_prefix == "traffic_light"
                else ["car", "bicycle", "pedestrian", "motorbike"],
                ignore_attributes=["cycle_state.without_rider"]
                if label_prefix == "autoware"
                else None,
            )
        )
        evaluation_config_dict.update(
            dict(
                allow_matching_unknown=True,
                merge_similar_labels=False,
                label_prefix=self.label_prefix,
                count_label_number=True,
            )
        )

        evaluation_config: PerceptionEvaluationConfig = PerceptionEvaluationConfig(
            dataset_paths=dataset_paths,
            frame_id=camera_type,
            result_root_directory=result_root_directory,
            evaluation_config_dict=evaluation_config_dict,
            load_raw_data=True,
        )

        _ = configure_logger(
            log_file_directory=evaluation_config.log_directory,
            console_log_level=logging.INFO,
            file_log_level=logging.INFO,
        )

        self.evaluator = PerceptionEvaluationManager(evaluation_config=evaluation_config)

    def callback(
        self,
        unix_time: int,
        estimated_objects: List[DynamicObject2D],
    ) -> None:
        # 現frameに対応するGround truthを取得
        ground_truth_now_frame = self.evaluator.get_ground_truth_now_frame(unix_time)

        # [Option] ROS側でやる（Map情報・Planning結果を用いる）UC評価objectを選別
        # ros_critical_ground_truth_objects : List[DynamicObject] = custom_critical_object_filter(
        #   ground_truth_now_frame.objects
        # )
        ros_critical_ground_truth_objects = ground_truth_now_frame.objects

        # 1 frameの評価
        target_labels = (
            ["green", "red", "yellow", "unknown"]
            if self.label_prefix == "traffic_light"
            else ["car", "bicycle", "pedestrian", "motorbike"]
        )
        ignore_attributes = (
            ["cycle_state.without_rider"] if self.label_prefix == "autoware" else None
        )
        matching_threshold_list = (
            None if self.evaluation_task == "classification2d" else [0.5, 0.5, 0.5, 0.5]
        )
        # 距離などでUC評価objectを選別するためのインターフェイス（PerceptionEvaluationManager初期化時にConfigを設定せず、関数受け渡しにすることで動的に変更可能なInterface）
        # どれを注目物体とするかのparam
        critical_object_filter_config: CriticalObjectFilterConfig = CriticalObjectFilterConfig(
            evaluator_config=self.evaluator.evaluator_config,
            target_labels=target_labels,
            ignore_attributes=ignore_attributes,
        )
        # Pass fail を決めるパラメータ
        frame_pass_fail_config: PerceptionPassFailConfig = PerceptionPassFailConfig(
            evaluator_config=self.evaluator.evaluator_config,
            target_labels=target_labels,
            matching_threshold_list=matching_threshold_list,
        )

        frame_result = self.evaluator.add_frame_result(
            unix_time=unix_time,
            ground_truth_now_frame=ground_truth_now_frame,
            estimated_objects=estimated_objects,
            ros_critical_ground_truth_objects=ros_critical_ground_truth_objects,
            critical_object_filter_config=critical_object_filter_config,
            frame_pass_fail_config=frame_pass_fail_config,
        )
        self.visualize(frame_result)

    def get_final_result(self) -> MetricsScore:
        """
        処理の最後に評価結果を出す
        """
        # number of fails for critical objects
        num_critical_fail: int = sum(
            map(
                lambda frame_result: frame_result.pass_fail_result.get_num_fail(),
                self.evaluator.frame_results,
            )
        )
        logging.info(f"Number of fails for critical objects: {num_critical_fail}")

        # scene metrics score
        final_metric_score = self.evaluator.get_scene_result()
        logging.info(f"final metrics result {final_metric_score}")
        return final_metric_score

    def visualize(self, frame_result: PerceptionFrameResult):
        """
        Frameごとの可視化
        """
        logging.info(
            f"{len(frame_result.pass_fail_result.tp_object_results)} TP objects, "
            f"{len(frame_result.pass_fail_result.fp_object_results)} FP objects, "
            f"{len(frame_result.pass_fail_result.fn_objects)} FN objects",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dataset_paths", nargs="+", type=str, help="The path(s) of dataset")
    parser.add_argument(
        "--use_tmpdir",
        action="store_true",
        help="Whether save results to temporal directory",
    )
    parser.add_argument(
        "-p",
        "--label_prefix",
        type=str,
        default="autoware",
        choices=["autoware", "traffic_light"],
        help="Whether evaluate Traffic Light Recognition",
    )
    parser.add_argument(
        "-c",
        "--camera_type",
        nargs="+",
        type=str.lower,
        default="cam_front",
        choices=[
            "cam_front",
            "cam_front_right",
            "cam_front_left",
            "cam_back",
            "cam_back_left",
            "cam_back_right",
            "cam_traffic_light_near",
            "cam_traffic_light_far",
        ],
        help="Name of camera data",
    )
    args = parser.parse_args()

    dataset_paths = args.dataset_paths
    if args.use_tmpdir:
        tmpdir = tempfile.TemporaryDirectory()
        result_root_directory: str = tmpdir.name
    else:
        result_root_directory: str = "data/result/{TIME}/"

    # ========================================= Detection =========================================
    print("=" * 50 + "Start Detection 2D" + "=" * 50)
    detection_lsim = PerceptionLSimMoc(
        dataset_paths,
        "detection2d",
        result_root_directory,
        args.label_prefix,
        args.camera_type,
    )

    for ground_truth_frame in detection_lsim.evaluator.ground_truth_frames:
        objects_with_difference = get_objects_with_difference2d(
            ground_truth_frame.objects, translate=(50, 50), label_to_unknown_rate=0.5
        )

        # To avoid case of there is no object
        if len(objects_with_difference) > 0:
            objects_with_difference.pop(0)
        detection_lsim.callback(
            ground_truth_frame.unix_time,
            objects_with_difference,
        )

    # final result
    detection_final_metric_score = detection_lsim.get_final_result()

    # Visualize all frame results.
    logging.info("Start visualizing detection results")
    if detection_lsim.evaluator.evaluator_config.load_raw_data:
        detection_lsim.evaluator.visualize_all()

    # Detection performance report
    detection_analyzer = PerceptionAnalyzer2D(detection_lsim.evaluator.evaluator_config)
    detection_analyzer.add(detection_lsim.evaluator.frame_results)
    score_df, conf_mat_df = detection_analyzer.analyze()
    if score_df is not None:
        logging.info(score_df.to_string())
    if conf_mat_df is not None:
        logging.info(conf_mat_df.to_string())

    # ========================================= Tracking =========================================
    print("=" * 50 + "Start Tracking 2D" + "=" * 50)
    tracking_lsim = PerceptionLSimMoc(
        dataset_paths,
        "tracking2d",
        result_root_directory,
        args.label_prefix,
        args.camera_type,
    )

    for ground_truth_frame in tracking_lsim.evaluator.ground_truth_frames:
        objects_with_difference = get_objects_with_difference2d(
            ground_truth_frame.objects, translate=(50, 50), label_to_unknown_rate=0.5
        )
        # To avoid case of there is no object
        if len(objects_with_difference) > 0:
            objects_with_difference.pop(0)
        tracking_lsim.callback(
            ground_truth_frame.unix_time,
            objects_with_difference,
        )

    # final result
    tracking_final_metric_score = tracking_lsim.get_final_result()
    if tracking_lsim.evaluator.evaluator_config.load_raw_data:
        tracking_lsim.evaluator.visualize_all()

    # Tracking performance report
    tracking_analyzer = PerceptionAnalyzer2D(tracking_lsim.evaluator.evaluator_config)
    tracking_analyzer.add(tracking_lsim.evaluator.frame_results)
    score_df, conf_mat_df = tracking_analyzer.analyze()
    if score_df is not None:
        logging.info(score_df.to_string())
    if conf_mat_df is not None:
        logging.info(conf_mat_df.to_string())

    # ========================================= Classification =========================================
    print("=" * 50 + "Start Classification 2D" + "=" * 50)
    classification_lsim = PerceptionLSimMoc(
        dataset_paths,
        "classification2d",
        result_root_directory,
        args.label_prefix,
        args.camera_type,
    )

    for ground_truth_frame in classification_lsim.evaluator.ground_truth_frames:
        objects_with_difference = get_objects_with_difference2d(
            ground_truth_frame.objects, label_to_unknown_rate=0.5
        )
        # To avoid case of there is no object
        if len(objects_with_difference) > 0:
            objects_with_difference.pop(0)
        classification_lsim.callback(
            ground_truth_frame.unix_time,
            objects_with_difference,
        )

    # final result
    classification_final_metric_score = classification_lsim.get_final_result()

    # Classification performance report
    classification_analyzer = PerceptionAnalyzer2D(classification_lsim.evaluator.evaluator_config)
    classification_analyzer.add(classification_lsim.evaluator.frame_results)
    score_df, conf_mat_df = classification_analyzer.analyze()
    if score_df is not None:
        logging.info(score_df.to_string())
    if conf_mat_df is not None:
        logging.info(conf_mat_df.to_string())

    # Clean up tmpdir
    if args.use_tmpdir:
        tmpdir.cleanup()
