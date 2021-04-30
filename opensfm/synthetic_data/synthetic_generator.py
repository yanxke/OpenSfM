from collections import defaultdict
from typing import Callable, Tuple, List, Dict, Any, Optional, Union

import cv2
import numpy as np
from opensfm import geo, pygeometry, pysfm, reconstruction as rc, types, pymap


def derivative(func: Callable, x: np.ndarray) -> np.ndarray:
    eps = 1e-10
    d = (func(x + eps) - func(x)) / eps
    d /= np.linalg.norm(d)
    return d


def samples_generator_random_count(count: int) -> np.ndarray:
    return np.random.rand(count)


def samples_generator_interval(
    start: float, length: float, interval: float, interval_noise: float
) -> np.ndarray:
    samples = np.linspace(start / length, 1, num=int(length / interval))
    samples += np.random.normal(
        0.0, float(interval_noise) / float(length), samples.shape
    )
    return samples


def generate_samples_and_local_frame(
    samples: np.ndarray, shape: Callable
) -> Tuple[np.ndarray, np.ndarray]:
    points = []
    tangents = []
    for i in samples:
        point = shape(i)
        points += [point]
        ex = derivative(shape, i)
        ez = np.array([ex[1], -ex[0]])
        tangents += [np.array([ez, ex])]
    return np.array(points), np.array(tangents)


def generate_samples_shifted(
    samples: np.ndarray, shape: Callable, shift: float
) -> np.ndarray:
    plane_points = []
    for i in samples:
        point = shape(i)
        tangent = derivative(shape, i)
        tangent = np.array([-tangent[1], tangent[0]])
        point += tangent * (shift / 2)
        plane_points += [point]
    return np.array(plane_points)


def generate_z_plane(
    samples: np.ndarray, shape: Callable, thickness: float
) -> np.ndarray:
    plane_points = []
    for i in samples:
        point = shape(i)
        tangent = derivative(shape, i)
        tangent = np.array([-tangent[1], tangent[0]])
        shift = tangent * ((np.random.rand() - 0.5) * thickness)
        point += shift
        plane_points += [point]
    plane_points = np.array(plane_points)
    return np.insert(plane_points, 2, values=0, axis=1)


def generate_xy_planes(
    samples: np.ndarray, shape: Callable, z_size: float, y_size: float
) -> np.ndarray:
    xy1 = generate_samples_shifted(samples, shape, y_size)
    xy2 = generate_samples_shifted(samples, shape, -y_size)
    xy1 = np.insert(xy1, 2, values=np.random.rand(xy1.shape[0]) * z_size, axis=1)
    xy2 = np.insert(xy2, 2, values=np.random.rand(xy2.shape[0]) * z_size, axis=1)
    return np.concatenate((xy1, xy2), axis=0)


def generate_street(
    samples: np.ndarray, shape: Callable, height: float, width: float
) -> Tuple[np.ndarray, np.ndarray]:
    walls = generate_xy_planes(samples, shape, height, width)
    floor = generate_z_plane(samples, shape, width)
    return walls, floor


def generate_cameras(
    samples: np.ndarray, shape: Callable, height: float
) -> Tuple[np.ndarray, np.ndarray]:
    positions, rotations = generate_samples_and_local_frame(samples, shape)
    positions = np.insert(positions, 2, values=height, axis=1)
    rotations = np.insert(rotations, 2, values=0, axis=2)
    rotations = np.insert(rotations, 1, values=np.array([0, 0, -1]), axis=1)
    return positions, rotations


def line_generator(length: float, point: np.ndarray) -> np.ndarray:
    x = point * length
    return np.transpose(np.array([x, 0]))


def ellipse_generator(x_size: float, y_size: float, point: float) -> np.ndarray:
    y = np.sin(point * 2 * np.pi) * y_size / 2
    x = np.cos(point * 2 * np.pi) * x_size / 2
    return np.transpose(np.array([x, y]))


def perturb_points(points: np.ndarray, sigmas: List[float]) -> None:
    eps = 1e-10
    for point in points:
        gaussian = np.array([max(s, eps) for s in sigmas])
        point += np.random.normal(0.0, gaussian, point.shape)


def generate_exifs(
    reconstruction: types.Reconstruction,
    gps_noise: Union[Dict[str, float], float],
    causal_gps_noise: bool = False,
) -> Dict[str, Any]:
    """Generate fake exif metadata from the reconstruction."""
    speed_ms = 10.0
    previous_pose = None
    previous_time = 0
    exifs = {}
    reference = geo.TopocentricConverter(0, 0, 0)
    for shot_name in sorted(reconstruction.shots.keys()):
        shot = reconstruction.shots[shot_name]
        exif = {}
        exif["width"] = shot.camera.width
        exif["height"] = shot.camera.height
        exif["focal_ratio"] = shot.camera.focal
        exif["camera"] = str(shot.camera.id)
        exif["make"] = str(shot.camera.id)

        pose = shot.pose.get_origin()

        if previous_pose is not None:
            previous_time += np.linalg.norm(pose - previous_pose) * speed_ms
        previous_pose = pose
        exif["capture_time"] = previous_time

        pose_arr = np.array([pose])
        perturb_points(pose_arr, [gps_noise, gps_noise, gps_noise])  # pyre-fixme [6]
        pose = pose_arr[0]

        _, _, _, comp = rc.shot_lla_and_compass(shot, reference)
        lat, lon, alt = reference.to_lla(*pose)

        exif["gps"] = {}
        exif["gps"]["latitude"] = lat
        exif["gps"]["longitude"] = lon
        exif["gps"]["altitude"] = alt
        exif["gps"]["dop"] = gps_noise
        exif["compass"] = {"angle": comp}
        exifs[shot_name] = exif
    return exifs


def perturb_rotations(rotations: np.ndarray, angle_sigma: float) -> None:
    for i in range(len(rotations)):
        rotation = rotations[i]
        rodrigues = cv2.Rodrigues(rotation)[0].ravel()
        angle = np.linalg.norm(rodrigues)
        angle_pertubed = angle + np.random.normal(0.0, angle_sigma)
        rodrigues *= float(angle_pertubed) / float(angle)
        rotations[i] = cv2.Rodrigues(rodrigues)[0]


def add_shots_to_reconstruction(
    shot_ids: List[str],
    positions: List[np.ndarray],
    rotations: List[np.ndarray],
    camera: pygeometry.Camera,
    reconstruction: types.Reconstruction,
):
    reconstruction.add_camera(camera)
    for shot_id, position, rotation in zip(shot_ids, positions, rotations):
        pose = pygeometry.Pose(rotation)
        pose.set_origin(position)
        reconstruction.create_shot(shot_id, camera.id, pose)


def add_points_to_reconstruction(
    points: np.ndarray, color: np.ndarray, reconstruction: types.Reconstruction
):
    shift = len(reconstruction.points)
    for i in range(points.shape[0]):
        point = reconstruction.create_point(str(shift + i), points[i, :])
        point.color = color


def add_rigs_to_reconstruction(
    shots: List[List[str]],
    positions: List[np.ndarray],
    rotations: List[np.ndarray],
    rig_cameras: List[pymap.RigCamera],
    reconstruction: types.Reconstruction,
):
    rec_rig_cameras = []
    for rig_camera in rig_cameras:
        if rig_camera.id not in reconstruction.rig_cameras:
            rec_rig_cameras.append(reconstruction.add_rig_camera(rig_camera))
        else:
            rec_rig_cameras.append(reconstruction.rig_cameras[rig_camera.id])

    for i, (i_shots, position, rotation) in enumerate(zip(shots, positions, rotations)):
        rig_instance = reconstruction.add_rig_instance(pymap.RigInstance(i))
        for j, s in enumerate(i_shots):
            rig_instance.add_shot(rec_rig_cameras[j], reconstruction.get_shot(s[0]))
        rig_instance.pose = pygeometry.Pose(rotation, -rotation.dot(position))


def create_reconstruction(
    points: List[np.ndarray],
    colors: List[np.ndarray],
    cameras: List[pygeometry.Camera],
    shot_ids: List[List[str]],
    positions: List[List[np.ndarray]],
    rotations: List[List[np.ndarray]],
    rig_shots: List[List[List[str]]],
    rig_positions: Optional[List[List[np.ndarray]]] = None,
    rig_rotations: Optional[List[List[np.ndarray]]] = None,
    rig_cameras: Optional[List[List[pymap.RigCamera]]] = None,
):
    reconstruction = types.Reconstruction()
    for point, color in zip(points, colors):
        add_points_to_reconstruction(point, color, reconstruction)

    for s_shot_ids, s_positions, s_rotations, s_cameras in zip(
        shot_ids, positions, rotations, cameras
    ):
        add_shots_to_reconstruction(
            s_shot_ids, s_positions, s_rotations, s_cameras, reconstruction
        )

    if rig_shots and rig_positions and rig_rotations and rig_cameras:
        for s_rig_shots, s_rig_positions, s_rig_rotations, s_rig_cameras in zip(
            rig_shots, rig_positions, rig_rotations, rig_cameras
        ):
            add_rigs_to_reconstruction(
                s_rig_shots,
                s_rig_positions,
                s_rig_rotations,
                s_rig_cameras,
                reconstruction,
            )
    return reconstruction


def generate_track_data(
    reconstruction: types.Reconstruction, maximum_depth: float, noise: float
) -> Tuple[
    Dict[str, np.ndarray],
    Dict[str, np.ndarray],
    Dict[str, np.ndarray],
    pysfm.TracksManager,
]:
    """Generate projection data from a reconstruction, considering a maximum
    viewing depth and gaussian noise added to the ideal projections.
    Returns feature/descriptor/color data per shot and a tracks manager object.
    """
    tracks_manager = pysfm.TracksManager()

    feature_data_type = np.float32
    desc_size = 128
    non_zeroes = 5
    track_descriptors = {}
    for track_index in reconstruction.points:
        descriptor = np.zeros(desc_size)
        for _ in range(non_zeroes):
            index = np.random.randint(0, desc_size)
            descriptor[index] = np.random.random() * 255
        track_descriptors[track_index] = descriptor.round().astype(feature_data_type)

    colors = {}
    features = {}
    descriptors = {}
    default_scale = 0.004
    for shot_index, shot in reconstruction.shots.items():
        # need to have these as we lost track of keys
        all_keys = list(reconstruction.points.keys())
        all_values = list(reconstruction.points.values())

        # temporary work on numpy array
        all_coordinates = [p.coordinates for p in all_values]
        projections = shot.project_many(np.array(all_coordinates))
        projections_inside = []
        descriptors_inside = []
        colors_inside = []
        for i, projection in enumerate(projections):
            if not _is_inside_camera(projection, shot.camera):
                continue
            original_key = all_keys[i]
            original_point = all_values[i]
            if not _is_in_front(original_point, shot):
                continue
            if not _check_depth(original_point, shot, maximum_depth):
                continue

            # add perturbation
            perturbation = float(noise) / float(
                max(shot.camera.width, shot.camera.height)
            )

            projection_arr = np.array([projection])
            perturb_points(
                projection_arr,
                # pyre-fixme [6]
                np.array([perturbation, perturbation]),
            )
            projection = projection_arr[0]

            projections_inside.append(np.hstack((projection, [default_scale])))
            descriptors_inside.append(track_descriptors[original_key])
            colors_inside.append(original_point.color)
            obs = pysfm.Observation(
                projection[0],
                projection[1],
                default_scale,
                original_point.color[0],
                original_point.color[1],
                original_point.color[2],
                len(projections_inside) - 1,
            )
            tracks_manager.add_observation(str(shot_index), str(original_key), obs)
        features[shot_index] = np.array(projections_inside)
        colors[shot_index] = np.array(colors_inside)
        descriptors[shot_index] = np.array(descriptors_inside)

    return features, descriptors, colors, tracks_manager


def _check_depth(point, shot, maximum_depth):
    return shot.pose.transform(point.coordinates)[2] < maximum_depth


def _is_in_front(point, shot):
    return (
        np.dot(
            (point.coordinates - shot.pose.get_origin()),
            shot.pose.get_rotation_matrix()[2],
        )
        > 0
    )


def _is_inside_camera(projection, camera):
    if camera.width > camera.height:
        return (-0.5 < projection[0] < 0.5) and (
            -float(camera.height) / float(2 * camera.width)
            < projection[1]
            < float(camera.height) / float(2 * camera.width)
        )
    else:
        return (-0.5 < projection[1] < 0.5) and (
            -float(camera.width) / float(2 * camera.height)
            < projection[0]
            < float(camera.width) / float(2 * camera.height)
        )
