# Getting Started

This page provides basic usage about yolov8-obb. For installation instructions, please see [install.md](./install.md).

# Train a model

**1. Prepare obb dataset files**

1.1 Make sure the labels format is [poly classname diffcult], e.g., You can set **diffcult=0**
```
  x1      y1       x2        y2       x3       y3       x4       y4       classname     diffcult

1686.0   1517.0   1695.0   1511.0   1711.0   1535.0   1700.0   1541.0   large-vehicle      1
```
![image](https://user-images.githubusercontent.com/72599120/159213229-b7c2fc5c-b140-4f10-9af8-2cbc405b0cd3.png)


**2. Prepare obb_kpt dataset files**

1.2 Make sure the labels format is [poly classname diffcult], e.g., You can set **diffcult=0**
```
x1      y1        x2      y2      x3      y3      x4      y4      classname    diffcult   kpt_x   kpt_y  kpt_diffcult

1686.0  1517.0  1695.0  1511.0  1711.0  1535.0  1700.0  1541.0  large-vehicle     1      1122.0   513.0      2.0 
```

1.3 Split the dataset. 
```shell
cd Yolov8_obb_Prune_Track
python DOTA_devkit/ImgSplit_multi_process.py
```
or Use the orignal dataset. 
```shell
cd Yolov8_obb_Prune_Track
```

1.3 Make sure your dataset structure same as:
```
└── datasets
    └── your data
          ├── images
              ├── train
                  |────1.jpg
                  |────...
                  └────10000.jpg
              ├── val
                  |────10001.jpg
                  |────...
                  └────11000.jpg
          ├── labelTxt
              ├── train
                    |────1.txt
                    |────...
                    └────10000.txt
              ├── val
                    |────10001.txt
                    |────...
                    └────11000.txt
```

```shell
python tools/mk_train.py --data_path  data_path
```

**Note:**
* DOTA is a high resolution image dataset, so it needs to be splited before training/testing to get better performance.

**2. Train**
```shell
#Train your obb dataset.
python train.py      --data 'data/yolov8obb_demo.yaml'   --hyp 'data/hyps/obb/hyp.finetune_dota.yaml' --cfg models/yolov8n.yaml   --epochs 300   --batch-size 8   --img 640   --device 0
#Train your obb_kpt dataset.
python train.py      --data 'data/yolov8obb_kpt_demo.yaml'   --hyp 'data/hyps/obb/hyp.finetune_dota_kpt.yaml' --cfg models/yaml/yolov8n_kpt.yaml   --epochs 300   --batch-size 8   --img 640   --device 0  
```

**3. val**
```shell
#Val your obb dataset.
python val.py --data data/yolov8obb_demo.yaml  --weights runs/train/exp/weights/best.pt --task 'val'  --img 640  --conf-thres=0.1  --iou-thres=0.1 --is_val
#Val your obb_kpt dataset.
python val.py --data data/yolov8obb_kpt_demo.yaml  --weights runs/train/exp103/weights/best.pt --task 'val' --img 640 --is_val --use_kpt --conf-thres=0.1  --iou-thres=0.1
```

**4. val_mmrotate**
需要在eval_rotate_PR_V8.py修改你的测试数据集，模型路径等
```shell
python eval_rotate_PR_V8.py 
```

**5. detcet**
```shell
#detect your obb dataset.
python detect.py --weights  runs/train/exp/weights/best.pt   --source dataset/your datafile/images/val/   --img 640 --device 0 --conf-thres 0.25 --iou-thres 0.2 
#detect your obb_kpt dataset.
python detect.py --weights runs/train/exp/weights/last.pt   --source dataset/obb_kpt data/images/val/   --img 640 --device 5 --conf-thres 0.25 --iou-thres 0.2 --use_kpt
```
**6. export**
```shell
python export.py --weights  runs/train/exp/weights/best.pt  --batch 1
```


# Prune Your Model,  only support obb model now.
**1.Sparity Train**
```shell
python train.py      --data 'data/yolov8obb_demo.yaml'   --hyp 'data/hyps/obb/hyp.finetune_dota.yaml' --cfg models/yaml/yolov8n.yaml   --epochs 1   --batch-size 8   --img 640      --device 2   --st --sr 0.0002
```
**2. Prune**
```shell
#剪枝，percent为剪枝比率
python prune.py --percent 0.3 --weights runs/train/exp/weights/last.pt --data data/yolov8obb_demo.yaml --cfg models/yaml/yolov8n.yaml  --is_val 
#如果传入close_head，则不对输出头部分的卷积层进行剪枝。
python prune.py --percent 0.3 --weights runs/train/exp/weights/last.pt --data data/yolov8obb_demo.yaml --cfg models/yaml/yolov8n.yaml  --is_val --close_head
```
**3. Finetune**
```shell
#微调
python prune_finetune.py --weights prune/pruned_model.pt --data data/data/yolov8obb_demo.yaml   --epochs 100 --imgsz 640 --batch-size 8
```

# Track
可选参数
video_path：需要预测的跟踪视频读取路径
video_save_path: 跟踪视频预测完的保存路径
video_fps：需要预测的跟踪视频读取帧数
weights: 旋转框检测模型路径
img_save_path：跟踪视频按照video_fps切分后保存图片的路径
track_type：跟踪类型，可选择bytetracker和strongsort
is_track_img：是否存储画有跟踪框的图片
track_img_path：画有跟踪框的图片的存储文件夹路径
is_track_det_img：是否存储画有检测框的图片
track_det_img_path：画有检测框的图片的存储文件夹路径
```shell
#跟踪
python track_predict.py  --video_path --video_fps --weights  --output
```