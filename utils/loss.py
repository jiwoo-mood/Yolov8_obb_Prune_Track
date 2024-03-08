# YOLOv5 🚀 by Ultralytics, GPL-3.0 license
"""
Loss functions
"""

import torch
import torch.nn as nn
import math
from utils.metrics import bbox_iou
from utils.torch_utils import is_parallel
import numpy as np
from utils.general import LOGGER, check_version
from utils.tal import TaskAlignedAssigner
import torch.nn.functional as F

OKS_SIGMA = np.array([.26, .25, .25, .35, .35, .79, .79, .72, .72, .62, .62, 1.07, 1.07, .87, .87, .89, .89]) / 10.0
# OKS_SIGMA = np.array([.25]) / 10.0
class KeypointLoss(nn.Module):
    """Criterion class for computing training losses."""

    def __init__(self, sigmas) -> None:
        """Initialize the KeypointLoss class."""
        super().__init__()
        self.sigmas = sigmas

    def forward(self, pred_kpts, gt_kpts, kpt_mask, area):
        """Calculates keypoint loss factor and Euclidean distance loss for predicted and actual keypoints."""
        d = (pred_kpts[..., 0] - gt_kpts[..., 0]) ** 2 + (pred_kpts[..., 1] - gt_kpts[..., 1]) ** 2
        # d = (pred_kpts[..., 0] - gt_kpts[..., 1]) ** 2 + (pred_kpts[..., 1] - gt_kpts[..., 0]) ** 2
        kpt_loss_factor = kpt_mask.shape[1] / (torch.sum(kpt_mask != 0, dim=1) + 1e-9)
        # e = d / (2 * (area * self.sigmas) ** 2 + 1e-9)  # from formula
        e = d / (2 * self.sigmas) ** 2 / (area + 1e-9) / 2  # from cocoeval
        return (kpt_loss_factor.view(-1, 1) * ((1 - torch.exp(-e)) * kpt_mask)).mean()


class v8DetectionLoss:
     # Compute losses
    def __init__(self, model, autobalance=False):
        device = next(model.parameters()).device  # get model device
        h = model.hyp  # hyperparameters
        
        # Define criteria
        BCEcls = nn.BCEWithLogitsLoss(pos_weight=torch.tensor([h['cls_pw']], device=device))
        #边框和角度loss
        self.kld_loss_n = KLDloss(1,fun='log1p')

        # Class label smoothing https://arxiv.org/pdf/1902.04103.pdf eqn 3
        self.cp, self.cn = smooth_BCE(eps=h.get('label_smoothing', 0.0))  # positive, negative BCE targets

        det = model.module.model[-1] if is_parallel(model) else model.model[-1]  # Detect() module

        self.no_box=det.no_box
        self.nc = det.nc
        self.assigner = TaskAlignedAssigner(topk=10, num_classes=self.nc, alpha=0.5, beta=6.0)
        self.device=device
        self.varifocal_loss=VarifocalLoss().to(device)
        self.fl=FocalLoss().to(device)
        self.qfl=QFocalLoss().to(device)

        self.reg_max=15
        self.stride = det.stride # tensor([8., 16., 32., ...])
        self.balance = {3: [4.0, 1.0, 0.4]}.get(det.nl, [4.0, 1.0, 0.25, 0.06, 0.02])  # P3-P7
        self.ssi = list(self.stride).index(16) if autobalance else 0  # stride 16 index
        self.BCEcls,  self.gr, self.hyp, self.autobalance = BCEcls, 1.0, h, autobalance
        for k in 'na', 'nc', 'nl':
            setattr(self, k, getattr(det, k))

    def __call__(self, p, targets,model_l='l1'):  # predictions, targets, model
        # box, cls, dfl loss
        lcls_loss = torch.zeros(1, device=self.device)
        box_loss = torch.zeros(1, device=self.device)
        dfl_loss = torch.zeros(1, device=self.device)
        #网络层输出
        feats = p[1] if isinstance(p, tuple) else p

        anchor_points, stride_tensor = make_anchors(feats, self.stride, 0.5)
        #dfl
        pred_distri,pred_theta,pred_scores = torch.cat([xi.view(feats[0].shape[0], self.no_box, -1) for xi in feats], 2).split(
            (64, 1,self.nc), 1)

        pred_scores = pred_scores.permute(0, 2, 1).contiguous()  #[16, 8400, n]
        pred_distri = pred_distri.permute(0, 2, 1).contiguous()  #[16, 8400, 64]
        pred_theta = pred_theta.permute(0, 2, 1).contiguous()    #[16, 8400, 1]

        #预测边框通过中心点anchor_points进行边框编码
        pred_bboxes = bbox_decode(anchor_points, pred_distri)  # xywh, (b, h*w, 4),#[16, 8400, 4]
        pred_theta   = (pred_theta.sigmoid()- 0.5) * math.pi
        pred_bboxes=torch.cat((pred_bboxes, pred_theta), -1)
 

        dtype = pred_scores.dtype   #torch.float16
        batch_size = pred_scores.shape[0]  #16
        imgsz = torch.tensor(feats[0].shape[2:], device=self.device, dtype=dtype) * self.stride[0]  # image size (h,w)#tensor([640., 640.], device='cuda:0', dtype=torch.float16)
        
        #将batch的gt维度进行合并
        targets = preprocess(targets.to(self.device), batch_size, self.device,scale_tensor=imgsz[[1, 0, 1, 0]]) #torch.Size([16, 2, 6])
        gt_labels, gt_bboxes = targets.split((1, 5), 2)  # cls, xyxy torch.Size([16, 2, 1]),torch.Size([16, 2, 5])
        mask_gt = gt_bboxes.sum(2, keepdim=True).gt_(0)  #torch.Size([16, 2, 1])

        #TAL动态匹配
        target_labels, target_bboxes, target_scores, fg_mask, _ = self.assigner(
            pred_scores.detach().sigmoid(), (pred_bboxes.detach() * stride_tensor).type(gt_bboxes.dtype),
            anchor_points * stride_tensor, gt_labels, gt_bboxes, mask_gt)

        
        target_scores_sum = max(target_scores.sum(), 1)
        target_labels = torch.where(target_scores > 0 , 1, 0)

        #分类vfl loss
        lcls_loss += self.varifocal_loss(pred_scores, target_scores, target_labels) / target_scores_sum  # VFL way
        #分类focal loss
        # lcls_loss += self.fl(pred_scores, target_labels.float())   # BCE
        #分类qfocal loss
        # lcls_loss += self.qfl(pred_scores, target_labels.float())  # BCE
        #分类eqfocal loss
        # lcls_loss += self.eqfl(pred_scores, target_labels.float())  # BCE

        if fg_mask.sum():
            #旋转边框值进行下采样，切记不能加入角度
            target_bboxes[:,:,:4] /= stride_tensor
            weight = target_scores.sum(-1)[fg_mask].unsqueeze(-1)
            # weight = target_scores.sum(-1)[fg_mask].unsqueeze(-1).pow(2)
            #边框loss计算
            probloss = probiou_loss(pred_bboxes[fg_mask], target_bboxes[fg_mask])
            box_loss +=(probloss* weight).sum() / (target_scores_sum*batch_size)

            #边框+角度loss
            # kldloss = self.kld_loss_n(pred_bboxes[fg_mask], target_bboxes[fg_mask])
            # box_loss +=(kldloss* weight).sum() / target_scores_sum

            #DFL loss
            target_ltrb = bbox2dist(anchor_points, target_bboxes[:,:,:4], self.reg_max)
            dfl_loss = df_loss(pred_distri[fg_mask].view(-1, self.reg_max + 1), target_ltrb[fg_mask]) * weight
            dfl_loss = dfl_loss.sum() / target_scores_sum
            dfl_loss=dfl_loss.unsqueeze(-1)

        #vfl
        lcls_loss *= self.hyp['cls']
        box_loss *=  self.hyp['box']
        dfl_loss *= self.hyp['dfl']

        return ( box_loss + lcls_loss+dfl_loss ) * batch_size, torch.cat(( box_loss,lcls_loss,dfl_loss)).detach()


class v8PoseLoss:
    # Compute losses
    def __init__(self, model, autobalance=False):
        device = next(model.parameters()).device  # get model device
        h = model.hyp  # hyperparameters
        # Define criteria
        BCEcls = nn.BCEWithLogitsLoss(pos_weight=torch.tensor([h['cls_pw']], device=device))
        #边框和角度loss
        self.kld_loss_n = KLDloss(1,fun='log1p')

        # Class label smoothing https://arxiv.org/pdf/1902.04103.pdf eqn 3
        self.cp, self.cn = smooth_BCE(eps=h.get('label_smoothing', 0.0))  # positive, negative BCE targets

        # # Focal loss
        # g = h['fl_gamma']  # focal loss gamma
        # if g > 0:
        #     BCEcls= FocalLoss(BCEcls, g)

        det = model.module.model[-1] if is_parallel(model) else model.model[-1]  # Detect() module

        self.no_box=det.no_box
        self.nc = det.nc
        self.assigner = TaskAlignedAssigner(topk=10, num_classes=self.nc, alpha=0.5, beta=6.0)
        self.device=device
        self.varifocal_loss=VarifocalLoss().to(device)

        self.reg_max=15
        self.stride = det.stride # tensor([8., 16., 32., ...])
        self.balance = {3: [4.0, 1.0, 0.4]}.get(det.nl, [4.0, 1.0, 0.25, 0.06, 0.02])  # P3-P7
        self.ssi = list(self.stride).index(16) if autobalance else 0  # stride 16 index
        self.BCEcls,  self.gr, self.hyp, self.autobalance = BCEcls, 1.0, h, autobalance
        for k in 'na', 'nc', 'nl':
            setattr(self, k, getattr(det, k))

        """
        keypoints
        sigmas值用来控制关键点的权重分布超参数，在yolopose中，每个关键点对应一个sigmas值。该值越大，表示对该关键点预测结果越不确定
        sigmas值的确定直接影响到模型的训练效果，一般来说，我们可以根据不同关键点的重要性来设定，对于难预测的关键点，可以选择较小的sigmas值，
        增大其权重，提高它们对模型训练的影响，同理对于容易预测的关键点，可选择较大的sigmas值，以减少权重，降低它们对模型训练的影响。
        """

        self.kpt_shape = model.module.model[-1].kpt_shape
        self.bce_pose = nn.BCEWithLogitsLoss()
        is_pose = self.kpt_shape == [1, 3]
        nkpt = self.kpt_shape[0]  # number of keypoints
        sigmas = torch.from_numpy(OKS_SIGMA).to(self.device) if is_pose else torch.ones(nkpt, device=self.device) / nkpt
        self.keypoint_loss = KeypointLoss(sigmas=sigmas)

    def __call__(self, preds, targets,model_l='l1'):  # predictions, targets, model
        # box, cls, dfl kpt kobj loss
        lcls_loss = torch.zeros(1, device=self.device)
        box_loss = torch.zeros(1, device=self.device)
        dfl_loss = torch.zeros(1, device=self.device)
        kpt_loss = torch.zeros(1, device=self.device)
        kobj_loss = torch.zeros(1, device=self.device)

        #网络层输出,特征图和预测关键点
        #train的预测输出
        if isinstance(preds[0], list):
            feats, pred_kpts = preds
        else:
            #val的预测输出
            feats=preds[0][1]
            pred_kpts=preds[1]

        #特征图上预测框编码
        anchor_points, stride_tensor = make_anchors(feats, self.stride, 0.5)
        #划分边框，角度和得分
        pred_distri,pred_theta,pred_scores = torch.cat([xi.view(feats[0].shape[0], self.no_box, -1) for xi in feats], 2).split(
            (64, 1,self.nc), 1)

        pred_scores = pred_scores.permute(0, 2, 1).contiguous()  #[16, 8400, n]
        pred_distri = pred_distri.permute(0, 2, 1).contiguous()  #[16, 8400, 64]
        pred_theta = pred_theta.permute(0, 2, 1).contiguous()    #[16, 8400, 1]
        pred_kpts = pred_kpts.permute(0, 2, 1).contiguous()

        #预测边框通过中心点anchor_points进行边框编码
        pred_bboxes = bbox_decode(anchor_points, pred_distri)  # xywh, (b, h*w, 4),#[16, 8400, 4]
        pred_theta   = (pred_theta.sigmoid()- 0.5) * math.pi
        pred_bboxes=torch.cat((pred_bboxes, pred_theta), -1)

   
        #关键点编码,将预测的关键点映射到对应的feats上的坐标点，即anchor的中心点，
        batch_size = pred_scores.shape[0]
        pred_kpts = self.kpts_decode(anchor_points, pred_kpts.view(batch_size, -1, *self.kpt_shape))  # (b, h*w, 17, 3)

        dtype = pred_scores.dtype   #torch.float16
        batch_size = pred_scores.shape[0]  #16
        imgsz = torch.tensor(feats[0].shape[2:], device=self.device, dtype=dtype) * self.stride[0]  # image size (h,w)#tensor([640., 640.], device='cuda:0', dtype=torch.float16)

        #获取每个目标所属的图片id
        batch_idx=targets[:,0].unsqueeze(1)
        rbox_target=targets[:,:7].clone()
        keypoints_targets = targets[:,7:].unsqueeze(1).clone()



        #将batch的gt维度进行合并
        targets = preprocess(rbox_target.to(self.device), batch_size, self.device,scale_tensor=imgsz[[1, 0, 1, 0]]) #torch.Size([16, 2, 6])
        gt_labels, gt_bboxes = targets.split((1, 5), 2)  # cls, xyxy torch.Size([16, 2, 1]),torch.Size([16, 2, 5])
        mask_gt = gt_bboxes.sum(2, keepdim=True).gt_(0)  #torch.Size([16, 2, 1])

        #TAL动态匹配
        target_labels, target_bboxes, target_scores, fg_mask, target_gt_idx = self.assigner(
            pred_scores.detach().sigmoid(), 
            (pred_bboxes.detach() * stride_tensor).type(gt_bboxes.dtype),
            anchor_points * stride_tensor, 
            gt_labels, 
            gt_bboxes, 
            mask_gt
        )

        
        target_scores_sum = max(target_scores.sum(), 1)
        target_labels = torch.where(target_scores > 0 , 1, 0)

        #分类loss
        lcls_loss += self.varifocal_loss(pred_scores, target_scores, target_labels) / target_scores_sum  # VFL way
        # lcls_loss += self.BCEcls(pred_scores, target_scores.to(dtype)).sum() / target_scores_sum  # BCE


        if fg_mask.sum():


            #旋转边框值进行下采样，切记不能加入角度
            target_bboxes[:,:,:4] /= stride_tensor
            weight = target_scores.sum(-1)[fg_mask].unsqueeze(-1)
            # weight = target_scores.sum(-1)[fg_mask].unsqueeze(-1).pow(2)
            #边框loss计算
            probloss = probiou_loss(pred_bboxes[fg_mask], target_bboxes[fg_mask])
            box_loss +=(probloss* weight).sum() / target_scores_sum

            #边框+角度loss
            # kldloss = self.kld_loss_n(pred_bboxes[fg_mask], target_bboxes[fg_mask])
            # box_loss +=(kldloss* weight).sum() / target_scores_sum

            #DFL loss
            target_ltrb = bbox2dist(anchor_points, target_bboxes[:,:,:4], self.reg_max)
            dfl_loss = df_loss(pred_distri[fg_mask].view(-1, self.reg_max + 1), target_ltrb[fg_mask]) * weight
            dfl_loss = dfl_loss.sum() / target_scores_sum
            dfl_loss=dfl_loss.unsqueeze(-1)

            # keypoints[..., 0] *= imgsz[1]
            # keypoints[..., 1] *= imgsz[0]
            kpt_loss, kobj_loss = self.calculate_keypoints_loss(
                fg_mask, target_gt_idx, keypoints_targets, batch_idx, stride_tensor, target_bboxes, pred_kpts
            )


        kpt_loss=kpt_loss.unsqueeze(0)
        kobj_loss=kobj_loss.unsqueeze(0)
        #rbox
        lcls_loss *= self.hyp['cls']
        box_loss  *= self.hyp['box']
        dfl_loss  *= self.hyp['dfl']
        #kpt
        kpt_loss  *= self.hyp['kpt']
        kobj_loss *= self.hyp['kobj']

        # return ( box_loss + lcls_loss ) * batch_size, torch.cat(( box_loss,lcls_loss)).detach()
        return ( box_loss + lcls_loss+dfl_loss+kpt_loss+kobj_loss ) * batch_size, torch.cat(( box_loss,lcls_loss,dfl_loss,kpt_loss,kobj_loss)).detach()

   
    @staticmethod
    def kpts_decode(anchor_points, pred_kpts):
        """Decodes predicted keypoints to image coordinates."""
        # import pdb
        # pdb.set_trace()
        y = pred_kpts.clone()
        y[..., :2] *= 2.0
        y[..., 0] += anchor_points[:, [0]] - 0.5
        y[..., 1] += anchor_points[:, [1]] - 0.5
        return y


    def calculate_keypoints_loss(
            self, masks, target_gt_idx, keypoints, batch_idx, stride_tensor, target_bboxes, pred_kpts
        ):
            """
            Calculate the keypoints loss for the model.

            This function calculates the keypoints loss and keypoints object loss for a given batch. The keypoints loss is
            based on the difference between the predicted keypoints and ground truth keypoints. The keypoints object loss is
            a binary classification loss that classifies whether a keypoint is present or not.

            Args:
                masks (torch.Tensor): Binary mask tensor indicating object presence, shape (BS, N_anchors).
                target_gt_idx (torch.Tensor): Index tensor mapping anchors to ground truth objects, shape (BS, N_anchors).
                keypoints (torch.Tensor): Ground truth keypoints, shape (N_kpts_in_batch, N_kpts_per_object, kpts_dim).
                batch_idx (torch.Tensor): Batch index tensor for keypoints, shape (N_kpts_in_batch, 1).
                stride_tensor (torch.Tensor): Stride tensor for anchors, shape (N_anchors, 1).
                target_bboxes (torch.Tensor): Ground truth boxes in (x1, y1, x2, y2) format, shape (BS, N_anchors, 4).
                pred_kpts (torch.Tensor): Predicted keypoints, shape (BS, N_anchors, N_kpts_per_object, kpts_dim).

            Returns:
                (tuple): Returns a tuple containing:
                    - kpts_loss (torch.Tensor): The keypoints loss.
                    - kpts_obj_loss (torch.Tensor): The keypoints object loss.
            """
            batch_idx = batch_idx.flatten()
            batch_size = len(masks)

            # Find the maximum number of keypoints in a single image
            max_kpts = torch.unique(batch_idx, return_counts=True)[1].max()

            # Create a tensor to hold batched keypoints
            batched_keypoints = torch.zeros(
                (batch_size, max_kpts, keypoints.shape[1], keypoints.shape[2]), device=keypoints.device
            )

            # TODO: any idea how to vectorize this?
            # Fill batched_keypoints with keypoints based on batch_idx
            for i in range(batch_size):
                keypoints_i = keypoints[batch_idx == i]
                batched_keypoints[i, : keypoints_i.shape[0]] = keypoints_i

            # Expand dimensions of target_gt_idx to match the shape of batched_keypoints
            target_gt_idx_expanded = target_gt_idx.unsqueeze(-1).unsqueeze(-1)

            # Use target_gt_idx_expanded to select keypoints from batched_keypoints
            selected_keypoints = batched_keypoints.gather(
                1, target_gt_idx_expanded.expand(-1, -1, keypoints.shape[1], keypoints.shape[2])
            )

            # Divide coordinates by stride
            selected_keypoints /= stride_tensor.view(1, -1, 1, 1)

            kpts_loss = 0
            kpts_obj_loss = 0

            if masks.any():
                gt_kpt = selected_keypoints[masks]
                #计算旋转框面积与角度无关，(x,y,w,h,r)
                area = target_bboxes[masks][:, 2:4].prod(1, keepdim=True)
                pred_kpt = pred_kpts[masks]
                kpt_mask = gt_kpt[..., 2] != 0 if gt_kpt.shape[-1] == 3 else torch.full_like(gt_kpt[..., 0], True)
                kpts_loss = self.keypoint_loss(pred_kpt, gt_kpt, kpt_mask, area)  # pose loss

                if pred_kpt.shape[-1] == 3:
                    kpts_obj_loss = self.bce_pose(pred_kpt[..., 2], kpt_mask.float())  # keypoint obj loss

            return kpts_loss, kpts_obj_loss



class KLDloss(nn.Module):

    def __init__(self, taf=1.0, fun="sqrt"):
        super(KLDloss, self).__init__()
        self.fun = fun
        self.taf = taf
        self.pi = 3.141592
    def forward(self, pred, target): # pred [[x,y,w,h,angle], ...]
        #assert pred.shape[0] == target.shape[0]
        # print('pred',pred.shape)
        pred = pred.view(-1, 5)
        target = target.view(-1, 5)

        delta_x = pred[:, 0] - target[:, 0]
        delta_y = pred[:, 1] - target[:, 1]
        
        pre_angle_radian = pred[:, 4]
        targrt_angle_radian = target[:, 4]


        # pre_angle_radian =  self.pi *(((pred[:, 4] * 180 / self.pi ) + 90)/180)
        # targrt_angle_radian = self.pi *(((target[:, 4] * 180 / self.pi ) + 90)/180)

        delta_angle_radian = pre_angle_radian - targrt_angle_radian

        kld =  0.5 * (
                        4 * torch.pow( ( delta_x.mul(torch.cos(targrt_angle_radian)) + delta_y.mul(torch.sin(targrt_angle_radian)) ), 2) / torch.pow(target[:, 2], 2)
                      + 4 * torch.pow( ( delta_y.mul(torch.cos(targrt_angle_radian)) - delta_x.mul(torch.sin(targrt_angle_radian)) ), 2) / torch.pow(target[:, 3], 2)
                     )\
             + 0.5 * (
                        torch.pow(pred[:, 3], 2) / torch.pow(target[:, 2], 2) * torch.pow(torch.sin(delta_angle_radian), 2)
                      + torch.pow(pred[:, 2], 2) / torch.pow(target[:, 3], 2) * torch.pow(torch.sin(delta_angle_radian), 2)
                      + torch.pow(pred[:, 3], 2) / torch.pow(target[:, 3], 2) * torch.pow(torch.cos(delta_angle_radian), 2)
                      + torch.pow(pred[:, 2], 2) / torch.pow(target[:, 2], 2) * torch.pow(torch.cos(delta_angle_radian), 2)
                     )\
             + 0.5 * (
                        torch.log(torch.pow(target[:, 3], 2) / torch.pow(pred[:, 3], 2))
                      + torch.log(torch.pow(target[:, 2], 2) / torch.pow(pred[:, 2], 2))
                     )\
             - 1.0

        

        if self.fun == "sqrt":
            kld = kld.clamp(1e-7).sqrt()
        elif self.fun == "log1p":
            kld = torch.log1p(kld.clamp(1e-7))
        else:
            pass

        kld_loss = 1 - 1 / (self.taf + kld)

        return kld_loss
    

def gbb_form(boxes):
    xy, wh, angle = torch.split(boxes, [2, 2, 1], dim=-1)
    return torch.concat([xy, wh.pow(2) / 12., angle], dim=-1)


def rotated_form(a_, b_, angles):
    cos_a = torch.cos(angles)
    sin_a = torch.sin(angles)
    a = a_ * torch.pow(cos_a, 2) + b_ * torch.pow(sin_a, 2)
    b = a_ * torch.pow(sin_a, 2) + b_ * torch.pow(cos_a, 2)
    c = (a_ - b_) * cos_a * sin_a
    return a, b, c


def probiou_loss(pred, target,  mode='l1'):
    """
        pred    -> a matrix [N,5](x,y,w,h,angle - in radians) containing ours predicted box ;in case of HBB angle == 0
        target  -> a matrix [N,5](x,y,w,h,angle - in radians) containing ours target    box ;in case of HBB angle == 0
        eps     -> threshold to avoid infinite values
        mode    -> ('l1' in [0,1] or 'l2' in [0,inf]) metrics according our paper

    """

    eps=1e-3
    gbboxes1 = gbb_form(pred)
    gbboxes2 = gbb_form(target)

    xy_p = pred[:, :2]
    xy_t = target[:, :2]
    beta=1.0 / 9.0
    # Smooth-L1 norm
    diff = torch.abs(xy_p - xy_t)
    xy_loss = torch.where(diff < beta, 0.5 * diff * diff / beta,
                          diff - 0.5 * beta).sum(dim=-1)

    x1, y1, a1_, b1_, c1_ = gbboxes1[:,
                                     0], gbboxes1[:,
                                                  1], gbboxes1[:,
                                                               2], gbboxes1[:,
                                                                            3], gbboxes1[:,
                                                                                         4]
    x2, y2, a2_, b2_, c2_ = gbboxes2[:,
                                     0], gbboxes2[:,
                                                  1], gbboxes2[:,
                                                               2], gbboxes2[:,
                                                                            3], gbboxes2[:,
                                                                                         4]

    a1, b1, c1 = rotated_form(a1_, b1_, c1_)
    a2, b2, c2 = rotated_form(a2_, b2_, c2_)

    t1 = 0.25 * ((a1 + a2) * (torch.pow(y1 - y2, 2)) + (b1 + b2) * (torch.pow(x1 - x2, 2))) + \
         0.5 * ((c1+c2)*(x2-x1)*(y1-y2))
    t2 = (a1 + a2) * (b1 + b2) - torch.pow(c1 + c2, 2)
    t3_ = (a1 * b1 - c1 * c1) * (a2 * b2 - c2 * c2)
    t3 = 0.5 * torch.log(t2 / (4 * torch.sqrt(F.relu(t3_)) + eps))

    B_d = (t1 / t2) + t3
    # B_d = t1 + t2 + t3

    B_d = torch.clip(B_d, min=eps, max=100.0)
    l1 = torch.sqrt(1.0 - torch.exp(-B_d) + eps)
    l_i = torch.pow(l1, 2.0)
    l2 = -torch.log(1.0 - l_i + eps)

    if mode == 'l1':
        probiou = l1
    if mode == 'l2':
        probiou = l2

    return probiou
    # return probiou+xy_loss

def xy_wh_r_2_xy_sigma(xywhr):
    """Convert oriented bounding box to 2-D Gaussian distribution.

    Args:
        xywhr (torch.Tensor): rbboxes with shape (N, 5).

    Returns:
        xy (torch.Tensor): center point of 2-D Gaussian distribution
            with shape (N, 2).
        sigma (torch.Tensor): covariance matrix of 2-D Gaussian distribution
            with shape (N, 2, 2).
    """
    _shape = xywhr.shape
    assert _shape[-1] == 5
    xy = xywhr[:, :2]
    wh = xywhr[:, 2:4].clamp(min=1e-7, max=1e7).reshape(-1, 2)
    r = xywhr[:, 4]
    cos_r = torch.cos(r)
    sin_r = torch.sin(r)
    R = torch.stack((cos_r, -sin_r, sin_r, cos_r), dim=-1).reshape(-1, 2, 2)
    S = 0.5 * torch.diag_embed(wh)

    sigma = R.bmm(S.square()).bmm(R.permute(0, 2,
                                            1)).reshape(_shape[:-1] + (2, 2))

    return xy, sigma

def kfiou_loss(pred,
               target,
               pred_decode=None,
               targets_decode=None,
               fun=None,
               beta=1.0 / 9.0,
               eps=1e-6):
    """Kalman filter IoU loss.

    Args:
        pred (torch.Tensor): Predicted bboxes.
        target (torch.Tensor): Corresponding gt bboxes.
        pred_decode (torch.Tensor): Predicted decode bboxes.
        targets_decode (torch.Tensor): Corresponding gt decode bboxes.
        fun (str): The function applied to distance. Defaults to None.
        beta (float): Defaults to 1.0/9.0.
        eps (float): Defaults to 1e-6.

    Returns:
        loss (torch.Tensor)
    """

    pred_decode=pred_decode.float()
    targets_decode=targets_decode.float()
    xy_p = pred[:, :2]
    xy_t = target[:, :2]
    _, Sigma_p = xy_wh_r_2_xy_sigma(pred_decode)
    _, Sigma_t = xy_wh_r_2_xy_sigma(targets_decode)
    Sigma_p=Sigma_p.float()
    Sigma_t=Sigma_t.float()
    # Smooth-L1 norm
    diff = torch.abs(xy_p - xy_t)
    xy_loss = torch.where(diff < beta, 0.5 * diff * diff / beta,
                          diff - 0.5 * beta).sum(dim=-1)
    Vb_p = 4 * Sigma_p.det().sqrt()
    Vb_t = 4 * Sigma_t.det().sqrt()

    K = Sigma_p.bmm((Sigma_p + Sigma_t).inverse())
    Sigma = Sigma_p - K.bmm(Sigma_p)
    Vb = 4 *Sigma.det().sqrt()
    Vb = torch.where(torch.isnan(Vb), torch.full_like(Vb, 0), Vb)
    KFIoU = Vb / (Vb_p + Vb_t - Vb + eps)

    if fun == 'ln':
        kf_loss = -torch.log(KFIoU + eps)
    elif fun == 'exp':
        kf_loss = torch.exp(1 - KFIoU) - 1
    else:
        kf_loss = 1 - KFIoU

    loss = (xy_loss + kf_loss).clamp(0)

    return loss


def smooth_BCE(eps=0.1):  # https://github.com/ultralytics/yolov3/issues/238#issuecomment-598028441
    # return positive, negative label smoothing BCE targets
    return 1.0 - 0.5 * eps, 0.5 * eps


class BCEBlurWithLogitsLoss(nn.Module):
    # BCEwithLogitLoss() with reduced missing label effects.
    def __init__(self, alpha=0.05):
        super().__init__()
        self.loss_fcn = nn.BCEWithLogitsLoss(reduction='none')  # must be nn.BCEWithLogitsLoss()
        self.alpha = alpha

    def forward(self, pred, true):
        loss = self.loss_fcn(pred, true)
        pred = torch.sigmoid(pred)  # prob from logits
        dx = pred - true  # reduce only missing label effects
        # dx = (pred - true).abs()  # reduce missing label and false label effects
        alpha_factor = 1 - torch.exp((dx - 1) / (self.alpha + 1e-4))
        loss *= alpha_factor
        return loss.mean()

class FocalLoss(nn.Module):
    """Wraps focal loss around existing loss_fcn(), i.e. criteria = FocalLoss(nn.BCEWithLogitsLoss(), gamma=1.5)."""

    def __init__(self, ):
        super().__init__()

    def forward(self, pred, label, gamma=1.5, alpha=0.25):
        """Calculates and updates confusion matrix for object detection/classification tasks."""
        loss = F.binary_cross_entropy_with_logits(pred, label, reduction='none')
        # p_t = torch.exp(-loss)
        # loss *= self.alpha * (1.000001 - p_t) ** self.gamma  # non-zero power for gradient stability

        # TF implementation https://github.com/tensorflow/addons/blob/v0.7.1/tensorflow_addons/losses/focal_loss.py
        pred_prob = pred.sigmoid()  # prob from logits
        p_t = label * pred_prob + (1 - label) * (1 - pred_prob)
        modulating_factor = (1.0 - p_t) ** gamma
        loss *= modulating_factor
        if alpha > 0:
            alpha_factor = label * alpha + (1 - label) * (1 - alpha)
            loss *= alpha_factor
        return loss.mean(1).sum()

class VarifocalLoss(nn.Module):
    # Varifocal loss by Zhang et al. https://arxiv.org/abs/2008.13367
    def __init__(self):
        super().__init__()

    def forward(self, pred_score, gt_score, label, alpha=1.25, gamma=2.0):

        weight = alpha * pred_score.sigmoid().pow(gamma) * (1 - label) + gt_score * label
        # weight = alpha * pred_score.sigmoid().pow(gamma) * (1 - label) + gt_score * label
        with torch.cuda.amp.autocast(enabled=False):
            loss = (F.binary_cross_entropy_with_logits(pred_score.float(), gt_score.float(), reduction="none") *
                    weight).sum()
            # loss = (F.binary_cross_entropy_with_logits(pred_score.float(), gt_score.float(), reduction="none") *
                    # weight).mean(1).sum()
        return loss

class QFocalLoss(nn.Module):
    # Wraps Quality focal loss around existing loss_fcn(), i.e. criteria = FocalLoss(nn.BCEWithLogitsLoss(), gamma=1.5)
    def __init__(self, ):
        super().__init__()


    def forward(self, pred, label, gamma=1.5, alpha=0.25):
        loss = F.binary_cross_entropy_with_logits(pred, label, reduction='none')

        pred_prob = pred.sigmoid()  # prob from logits
        alpha_factor = label * alpha + (1 - label) * (1 - alpha)
        modulating_factor = torch.abs(label - pred_prob) ** gamma
        loss *= alpha_factor * modulating_factor

        return loss.mean(1).sum()   

def df_loss(pred_dist, target):
    # Return sum of left and right DFL losses
    # Distribution Focal Loss (DFL) proposed in Generalized Focal Loss https://ieeexplore.ieee.org/document/9792391
    tl = target.long()  # target left
    tr = tl + 1  # target right
    wl = tr - target  # weight left
    wr = 1 - wl  # weight right
    return (F.cross_entropy(pred_dist, tl.view(-1), reduction="none").view(tl.shape) * wl +
            F.cross_entropy(pred_dist, tr.view(-1), reduction="none").view(tl.shape) * wr).mean(-1, keepdim=True)

def bbox2dist(anchor_points, bbox, reg_max):
    """Transform bbox(xyxy) to dist(ltrb)."""
    xy, wh = torch.split(bbox, 2, -1)
    x2y2=(2*xy+wh)/2
    x1y1=(2*xy-wh)/2

    return torch.cat((anchor_points - x1y1, x2y2 - anchor_points), -1).clamp(0, reg_max - 0.01)  # dist (lt, rb)


TORCH_1_10 = check_version(torch.__version__, '1.10.0')
def make_anchors(feats, strides, grid_cell_offset=0.5):
    """Generate anchors from features."""
    anchor_points, stride_tensor = [], []
    assert feats is not None
    dtype, device = feats[0].dtype, feats[0].device
    for i, stride in enumerate(strides):
        #feats[i],[16, 6, 80, 80],[16, 6, 40, 40],[16, 6, 20, 20]
        _, _, h, w = feats[i].shape
        sx = torch.arange(end=w, device=device, dtype=dtype) + grid_cell_offset  # shift x
        sy = torch.arange(end=h, device=device, dtype=dtype) + grid_cell_offset  # shift y
        sy, sx = torch.meshgrid(sy, sx, indexing='ij') if TORCH_1_10 else torch.meshgrid(sy, sx)
        anchor_points.append(torch.stack((sx, sy), -1).view(-1, 2))
        stride_tensor.append(torch.full((h * w, 1), stride, dtype=dtype, device=device))

    return torch.cat(anchor_points), torch.cat(stride_tensor)

def dist2bbox(distance, anchor_points, xywh=True, dim=-1):
    """Transform distance(ltrb) to box(xywh or xyxy)."""
    # lt, rb,theta = torch.split(distance, [2,2,1], dim)
    # import pdb
    # pdb.set_trace()
    lt, rb = torch.split(distance, 2, dim)
    x1y1 = anchor_points - lt
    x2y2 = anchor_points + rb
    if xywh:
        c_xy = (x1y1 + x2y2) / 2
        wh = x2y2 - x1y1
        # c_xy      = c_xy.sigmoid() * 2. - 0.5
        # wh      = (wh.sigmoid() * 2) ** 2 
        
        return torch.cat((c_xy, wh), dim)  # xywh bbox
    return torch.cat((x1y1, x2y2), dim)  # xyxy bbox

def bbox_decode(anchor_points, pred_dist):
    device=pred_dist.device
    proj = torch.arange(16, dtype=torch.float, device=device)
    b, a, c = pred_dist.shape  # batch, anchors, channels
    pred_dist = pred_dist.view(b, a, 4, c // 4).softmax(3).matmul(proj.type(pred_dist.dtype))
    return dist2bbox(pred_dist, anchor_points, xywh=True)

def preprocess( targets, batch_size, device,scale_tensor):

    # import pdb
    # pdb.set_trace()
    if targets.shape[0] == 0:
        out = torch.zeros(batch_size, 0, 6, device=device)
    else:
        i = targets[:, 0]  # image index
        _, counts = i.unique(return_counts=True)
        out = torch.zeros(batch_size, counts.max(), 6, device=device)
        for j in range(batch_size):
            matches = i == j
            n = matches.sum()
            if n:
                out[j, :n] = targets[matches, 1:]
   

        # out[..., 1:5] = out[..., 1:5].mul_(scale_tensor)
    return out


