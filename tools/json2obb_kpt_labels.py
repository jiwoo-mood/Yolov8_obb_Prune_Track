import json
import os
import  numpy as np
import math

# 转换成四点坐标
def rotatePoint(xc, yc, xp, yp, theta):
    xoff = xp - xc;
    yoff = yp - yc;
    cosTheta = math.cos(theta)
    sinTheta = math.sin(theta)
    pResx = cosTheta * xoff + sinTheta * yoff
    pResy = - sinTheta * xoff + cosTheta * yoff
    return str(int(xc + pResx)), str(int(yc + pResy))


def check_points_in_rotated_boxes(points, boxes):
    """Check whether point is in rotated boxes

    Args:
        points (tensor): (1, L, 2) anchor points
        boxes (tensor): [B, N, 5] gt_bboxes
        eps (float): default 1e-9

    Returns:
        is_in_box (tensor): (B, N, L)

    """
    a = np.array(boxes[0])
    b = np.array(boxes[1])
    c = np.array(boxes[2])
    d = np.array(boxes[3])
    ab = b - a
    ad = d - a
    # [B, N, L, 2]
    ap = points - a
    # [B, N, L]
    norm_ab = np.sum(ab * ab)
    # [B, N, L]
    norm_ad = np.sum(ad * ad)
    # [B, N, L] dot product
    ap_dot_ab = np.sum(ap * ab)
    # [B, N, L] dot product
    ap_dot_ad = np.sum(ap * ad)
    # [B, N, L] <A, B> = |A|*|B|*cos(theta)
    is_in_box = (ap_dot_ab >= 0) & (ap_dot_ab <= norm_ab) & (ap_dot_ad >= 0) & (
            ap_dot_ad <= norm_ad)
    return is_in_box


rotate_obj_cls_list=['car']
rotate_kpt_cls_list=['car_head']
#融合yolov8obb和yolov8-pose的标注格式生成标签txt文件，在yolov8obb的每个目标后添加该目标类的关键点，本脚本只添加唯一关键点以此来确定目标的朝向，如车头，船头等
#如果要检测旋转框内多个关键点，需要按关键点的顺序对应摆放，或者根据类别来判断。
def convert_coco_to_yolov8obb(annotations_dir, txt_output_dir):
    files = os.listdir(annotations_dir)
    print('files',files)
    for file in files:
        print('file', file)
        file_name=file.split('.')[0]
        output = txt_output_dir +'/'+ file_name + '.txt'
        txt_file = open(output, 'w')

        with open(annotations_dir+'/'+file, 'r') as f:
            data = json.load(f)

        if not os.path.exists(txt_output_dir):
            os.makedirs(txt_output_dir)
        print('data',data)
        #把json里面所有的旋转框的关键点单独存放
        rotate_list=[]
        kpt_list=[]
        for i in range(len(data['shapes'])):
            for j in range(len(rotate_obj_cls_list)):
                if data['shapes'][i]['label'] == rotate_obj_cls_list[j]:
                    rotate_list.append([data['shapes'][i]['points'],data['shapes'][i]['label'],j])
            if data['shapes'][i]['label'] in rotate_kpt_cls_list:
                kpt_list.append(data['shapes'][i]['points'])
        #再匹配对应的关键点所属的旋转框并保存
        for i in range(len(rotate_list)):
            for j in range(len(kpt_list)):
                if check_points_in_rotated_boxes(kpt_list[j],rotate_list[i][0])==True:
                    x0 = int(rotate_list[i][0][0][0])
                    y0 = int(rotate_list[i][0][0][1])
                    x1 = int(rotate_list[i][0][1][0])
                    y1 = int(rotate_list[i][0][1][1])
                    x2 = int(rotate_list[i][0][2][0])
                    y2 = int(rotate_list[i][0][2][1])
                    x3 = int(rotate_list[i][0][3][0])
                    y3 = int(rotate_list[i][0][3][1])
                    cls = rotate_list[i][1]
                    id = rotate_list[i][2]
                    kpt0 = int(kpt_list[j][0][0])
                    kpt1 = int(kpt_list[j][0][1])

                    txt_file.write("{} {} {} {} {} {} {} {} {} {} {} {} 2\n".format(x0, y0, x1, y1, x2, y2, x3, y3, cls, id,kpt0,kpt1))
                print(check_points_in_rotated_boxes(kpt_list[j],rotate_list[i][0]))





if __name__ == '__main__':
    annotations_dir = r"./json_labels"
    txt_output_dir = r"./txt_labels"
    convert_coco_to_yolov8obb(annotations_dir, txt_output_dir)