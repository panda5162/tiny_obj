import os
import config
import json
import tensorflow as tf
import numpy as np
from collections import defaultdict
slim = tf.contrib.slim

class Reader:
    def __init__(self, mode, data_dir, anchors_path, num_classes, input_shape = 416, max_boxes = 20):
        """
        Introduction
        ------------
            构造函数
        Parameters
        ----------
            data_dir: 文件路径
            mode: 数据集模式
            anchors: 数据集聚类得到的anchor
            num_classes: 数据集图片类别数量
            input_shape: 图像输入模型的大小
            max_boxes: 每张图片最大的box数量
            jitter: 随机长宽比系数
        """
        self.data_dir = data_dir
        self.input_shape = input_shape
        self.max_boxes = max_boxes
        self.mode = mode
        self.anchors_path = anchors_path
        self.anchors = self._get_anchors()
        self.num_classes = num_classes
        file_pattern = self.data_dir + "/tfrecords/" + self.mode + "-*"  + '-of-00016'
        self.TfrecordFile = tf.gfile.Glob(file_pattern)
        self.class_names = self._get_class(config.classes_path)

        # if len(self.TfrecordFile) == 0:
        #     self.convert_to_tfrecord(self.data_dir, tfrecord_num)


    def _get_anchors(self):
        """
        Introduction
        ------------
            获取anchors
        Returns
        -------
            anchors: anchor数组
        """
        anchors_path = os.path.expanduser(self.anchors_path)
        with open(anchors_path) as f:
            anchors = f.readline()
        anchors = [float(x) for x in anchors.split(',')]
        return np.array(anchors).reshape(-1, 2)


    def Preprocess_true_boxes(self, true_boxes):
        """
        Introduction
        ------------
            对训练数据的ground truth box进行预处理
        Parameters
        ----------
            true_boxes: ground truth box 形状为[boxes, 5], x_min, y_min, x_max, y_max, class_id
        """
        num_layers = len(self.anchors) // 3
        anchor_mask = [[6, 7, 8], [3, 4, 5], [0, 1, 2]]
        true_boxes = np.array(true_boxes, dtype='float32')
        input_shape = np.array([self.input_shape, self.input_shape], dtype='int32')
        boxes_xy = (true_boxes[..., 0:2] + true_boxes[..., 2:4]) // 2.
        boxes_wh = true_boxes[..., 2:4] - true_boxes[..., 0:2]
        true_boxes[..., 0:2] = boxes_xy / input_shape[::-1]
        true_boxes[..., 2:4] = boxes_wh / input_shape[::-1]


        grid_shapes = [input_shape // 32, input_shape // 16, input_shape // 8]
        y_true = [np.zeros((grid_shapes[l][0], grid_shapes[l][1], len(anchor_mask[l]), 5 + self.num_classes), dtype='float32') for l in range(num_layers)]
        # 这里扩充维度是为了后面应用广播计算每个图中所有box的anchor互相之间的iou
        anchors = np.expand_dims(self.anchors, 0)
        anchors_max = anchors / 2.
        anchors_min = -anchors_max
        # 因为之前对box做了padding, 因此需要去除全0行
        valid_mask = boxes_wh[..., 0] > 0
        wh = boxes_wh[valid_mask]
        # 为了应用广播扩充维度
        wh = np.expand_dims(wh, -2)
        # wh 的shape为[box_num, 1, 2]
        boxes_max = wh / 2.
        boxes_min = -boxes_max

        intersect_min = np.maximum(boxes_min, anchors_min)
        intersect_max = np.minimum(boxes_max, anchors_max)
        intersect_wh = np.maximum(intersect_max - intersect_min, 0.)
        intersect_area = intersect_wh[..., 0] * intersect_wh[..., 1]
        box_area = wh[..., 0] * wh[..., 1]
        anchor_area = anchors[..., 0] * anchors[..., 1]
        iou = intersect_area / (box_area + anchor_area - intersect_area)

        # 找出和ground truth box的iou最大的anchor box, 然后将对应不同比例的负责该ground turth box 的位置置为ground truth box坐标
        best_anchor = np.argmax(iou, axis = -1)
        for t, n in enumerate(best_anchor):
            for l in range(num_layers):
                if n in anchor_mask[l]:
                    i = np.floor(true_boxes[t, 0] * grid_shapes[l][1]).astype('int32')
                    j = np.floor(true_boxes[t, 1] * grid_shapes[l][0]).astype('int32')
                    k = anchor_mask[l].index(n)
                    c = true_boxes[t, 4].astype('int32')
                    y_true[l][j, i, k, 0:4] = true_boxes[t, 0:4]
                    y_true[l][j, i, k, 4] = 1.
                    y_true[l][j, i, k, 5 + c] = 1.
        return y_true[0], y_true[1], y_true[2]


    def Preprocess(self, image, bbox):
        """
        Introduction
        ------------
            对图片进行预处理，增强数据集
        Parameters
        ----------
            image: tensorflow解析的图片
            bbox: 图片中对应的box坐标
        """
        image_width, image_high = tf.cast(tf.shape(image)[1], tf.float32), tf.cast(tf.shape(image)[0], tf.float32)
        input_width = tf.cast(self.input_shape, tf.float32)
        input_high = tf.cast(self.input_shape, tf.float32)
        new_high = image_high * tf.minimum(input_width / image_width, input_high / image_high)
        new_width = image_width * tf.minimum(input_width / image_width, input_high / image_high)
        # 将图片按照固定长宽比进行padding缩放
        dx = (input_width - new_width) / 2
        dy = (input_high - new_high) / 2
        image = tf.image.resize_images(image, [tf.cast(new_high, tf.int32), tf.cast(new_width, tf.int32)], method = tf.image.ResizeMethod.BICUBIC)
        new_image = tf.image.pad_to_bounding_box(image, tf.cast(dy, tf.int32), tf.cast(dx, tf.int32), tf.cast(input_high, tf.int32), tf.cast(input_width, tf.int32))
        image_ones = tf.ones_like(image)
        image_ones_padded = tf.image.pad_to_bounding_box(image_ones, tf.cast(dy, tf.int32), tf.cast(dx, tf.int32), tf.cast(input_high, tf.int32), tf.cast(input_width, tf.int32))
        image_color_padded = (1 - image_ones_padded) * 128
        image = image_color_padded + new_image
        # 矫正bbox坐标
        xmin, ymin, xmax, ymax, label = tf.split(value = bbox, num_or_size_splits=5, axis = 1)
        xmin = xmin * new_width / image_width + dx
        xmax = xmax * new_width / image_width + dx
        ymin = ymin * new_high / image_high + dy
        ymax = ymax * new_high / image_high + dy
        bbox = tf.concat([xmin, ymin, xmax, ymax, label], 1)
        if self.mode == 'train':
            # 随机左右翻转图片
            def _flip_left_right_boxes(boxes):
                xmin, ymin, xmax, ymax, label = tf.split(value = boxes, num_or_size_splits = 5, axis = 1)
                flipped_xmin = tf.subtract(input_width, xmax)
                flipped_xmax = tf.subtract(input_width, xmin)
                flipped_boxes = tf.concat([flipped_xmin, ymin, flipped_xmax, ymax, label], 1)
                return flipped_boxes
            flip_left_right = tf.greater(tf.random_uniform([], dtype = tf.float32, minval = 0, maxval = 1), 0.5)
            image = tf.cond(flip_left_right, lambda : tf.image.flip_left_right(image), lambda : image)
            bbox = tf.cond(flip_left_right, lambda: _flip_left_right_boxes(bbox), lambda: bbox)
        # 将图片归一化到0和1之间
        image = image / 255.
        image = tf.clip_by_value(image, clip_value_min = 0.0, clip_value_max = 1.0)
        bbox = tf.clip_by_value(bbox, clip_value_min = 0, clip_value_max = input_width - 1)
        bbox = tf.cond(tf.greater(tf.shape(bbox)[0], config.max_boxes), lambda: bbox[:config.max_boxes], lambda: tf.pad(bbox, paddings = [[0, config.max_boxes - tf.shape(bbox)[0]], [0, 0]], mode = 'CONSTANT'))
        return image, bbox


    def slim_get_split(self, serialized_example):

        """""
               Introduction
               ------------
                   解析tfRecord数据
               Parameters
               ----------
                   serialized_example: 序列化的每条数据
               """
        features = tf.parse_single_example(
            serialized_example,
            features={
                'image/encoded': tf.FixedLenFeature([], dtype=tf.string),
                'image/object/bbox/xmin': tf.VarLenFeature(dtype=tf.float32),
                'image/object/bbox/xmax': tf.VarLenFeature(dtype=tf.float32),
                'image/object/bbox/ymin': tf.VarLenFeature(dtype=tf.float32),
                'image/object/bbox/ymax': tf.VarLenFeature(dtype=tf.float32),
                'image/object/bbox/label': tf.VarLenFeature(dtype=tf.float32)
            }
        )
        image = tf.image.decode_jpeg(features['image/encoded'], channels=3)
        image = tf.image.convert_image_dtype(image, tf.uint8)
        xmin = tf.expand_dims(features['image/object/bbox/xmin'].values, axis=0)
        ymin = tf.expand_dims(features['image/object/bbox/ymin'].values, axis=0)
        xmax = tf.expand_dims(features['image/object/bbox/xmax'].values, axis=0)
        ymax = tf.expand_dims(features['image/object/bbox/ymax'].values, axis=0)
        label = tf.expand_dims(features['image/object/bbox/label'].values, axis=0)
        bbox = tf.concat(axis=0, values=[xmin, ymin, xmax, ymax, label])
        bbox = tf.transpose(bbox, [1, 0])
        image, bbox = self.Preprocess(image, bbox)
        bbox_true_13, bbox_true_26, bbox_true_52 = tf.py_func(self.Preprocess_true_boxes, [bbox],
                                                              [tf.float32, tf.float32, tf.float32])
        return image, bbox, bbox_true_13, bbox_true_26, bbox_true_52

        # """
        # 解析tfRecord数据
        # """
        # # Features in Pascal VOC TFRecords.
        # keys_to_features = {
        #     'image/encoded': tf.FixedLenFeature((), tf.string, default_value=''),
        #     'image/format': tf.FixedLenFeature((), tf.string, default_value='jpeg'),
        #     'image/filename': tf.FixedLenFeature((), tf.string, default_value=''),
        #     'image/height': tf.FixedLenFeature([1], tf.int64),
        #     'image/width': tf.FixedLenFeature([1], tf.int64),
        #     'image/channels': tf.FixedLenFeature([1], tf.int64),
        #     'image/shape': tf.FixedLenFeature([3], tf.int64),
        #     'image/object/bbox/xmin': tf.VarLenFeature(dtype=tf.float32),
        #     'image/object/bbox/ymin': tf.VarLenFeature(dtype=tf.float32),
        #     'image/object/bbox/xmax': tf.VarLenFeature(dtype=tf.float32),
        #     'image/object/bbox/ymax': tf.VarLenFeature(dtype=tf.float32),
        #     'image/object/bbox/label': tf.VarLenFeature(dtype=tf.int64),
        #     'image/object/bbox/difficult': tf.VarLenFeature(dtype=tf.int64),
        #     'image/object/bbox/truncated': tf.VarLenFeature(dtype=tf.int64),
        # }
        # items_to_handlers = {
        #     'image': slim.tfexample_decoder.Image('image/encoded', 'image/format'),
        #     'filename': slim.tfexample_decoder.Tensor('image/filename'),
        #     'shape': slim.tfexample_decoder.Tensor('image/shape'),
        #     'object/bbox/xmin': slim.tfexample_decoder.Tensor('image/object/bbox/xmin'),
        #     'object/bbox/ymin': slim.tfexample_decoder.Tensor('image/object/bbox/ymin'),
        #     'object/bbox/xmax': slim.tfexample_decoder.Tensor('image/object/bbox/xmax'),
        #     'object/bbox/ymax': slim.tfexample_decoder.Tensor('image/object/bbox/ymax'),
        #     'object/label': slim.tfexample_decoder.Tensor('image/object/bbox/label'),
        #     'object/difficult': slim.tfexample_decoder.Tensor('image/object/bbox/difficult'),
        #     'object/truncated': slim.tfexample_decoder.Tensor('image/object/bbox/truncated'),
        # }
        # decoder = slim.tfexample_decoder.TFExampleDecoder(keys_to_features, items_to_handlers)
        #
        # dataset = slim.dataset.Dataset(
        #     data_sources=file_pattern,
        #     reader=tf.TFRecordReader,
        #     decoder=decoder,
        #     num_samples=100,
        #     items_to_descriptions=None,
        #     num_classes=21,
        #     labels_to_names=None)
        #
        # with tf.name_scope('dataset_data_provider'):
        #     provider = slim.dataset_data_provider.DatasetDataProvider(
        #         dataset,
        #         num_readers=2,
        #         common_queue_capacity=32,
        #         common_queue_min=8,
        #         shuffle=True,
        #         num_epochs=1)
        # [image, xmin, ymin, xmax, ymax, label] = provider.get(
        #     ['image',
        #      'object/bbox/xmin',
        #      'object/bbox/ymin',
        #      'object/bbox/xmax',
        #      'object/bbox/ymax',
        #      'object/label'])
        # bbox = tf.concat(axis=0, values=[xmin, ymin, xmax, ymax, label])
        # bbox = tf.transpose(bbox, [1, 0])
        # image, bbox = self.Preprocess(image, bbox)
        # bbox_true_13, bbox_true_26, bbox_true_52 = tf.py_func(self.Preprocess_true_boxes, [bbox],
        #                                                       [tf.float32, tf.float32, tf.float32])
        # return image, bbox, bbox_true_13, bbox_true_26, bbox_true_52


    def build_dataset(self, batch_size):
        """
        Introduction
        ------------
            建立数据集dataset
        Parameters
        ----------
            batch_size: batch大小
        Return
        ------
            dataset: 返回tensorflow的dataset
        """
        dataset = tf.data.TFRecordDataset(filenames=self.TfrecordFile)
        dataset = dataset.map(self.slim_get_split, num_parallel_calls=10)
        if self.mode == 'train':
            dataset = dataset.repeat().shuffle(9000).batch(batch_size).prefetch(batch_size)
        else:
            dataset = dataset.repeat().batch(batch_size).prefetch(batch_size)
        return dataset
