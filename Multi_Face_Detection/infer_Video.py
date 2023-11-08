import argparse
import os

import cv2
import numpy as np
import torch
import keyboard

from utils.utils import generate_bbox, py_nms, convert_to_square
from utils.utils import pad, calibrate_box, processed_image

parser = argparse.ArgumentParser()
parser.add_argument('--model_path', type=str, default='infer_models',      help='PNet、RNet、ONet三个模型文件存在的文件夹路径')
args = parser.parse_args()

device = torch.device("cuda")

# 获取P模型
pnet = torch.jit.load(os.path.join(args.model_path, 'PNet.pth'))
pnet.to(device)
softmax_p = torch.nn.Softmax(dim=0)
pnet.eval()

# 获取R模型
rnet = torch.jit.load(os.path.join(args.model_path, 'RNet.pth'))
rnet.to(device)
softmax_r = torch.nn.Softmax(dim=-1)
rnet.eval()

# 获取O模型
onet = torch.jit.load(os.path.join(args.model_path, 'ONet.pth'))
onet.to(device)
softmax_o = torch.nn.Softmax(dim=-1)
onet.eval()

# 输出人脸图像的文件夹路径
output_face_folder = 'face'
if not os.path.exists(output_face_folder):
    os.makedirs(output_face_folder)

def save_detected_faces(frame, boxes_c, landmarks, frame_count, fps):
    seconds = frame_count / fps
    # 创建一个新的文件夹来存储当前秒的检测到的人脸图像
    frame_face_folder = os.path.join(output_face_folder, f'second_{int(seconds):04d}')
    os.makedirs(frame_face_folder, exist_ok=True)

    for i in range(boxes_c.shape[0]):
        bbox = boxes_c[i, :4]
        corpbbox = [int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])]
        face_img = frame[corpbbox[1]:corpbbox[3], corpbbox[0]:corpbbox[2]]
        # 检查是否有检测到的人脸
        if face_img.size > 0:
            # 保存检测到的人脸图像
            face_filename = os.path.join(frame_face_folder, f"face_{i:02d}.png")
            cv2.imwrite(face_filename, face_img)

# 使用PNet模型预测
def predict_pnet(infer_data):
    # 添加待预测的图片
    infer_data = torch.tensor(infer_data, dtype=torch.float32, device=device)
    infer_data = torch.unsqueeze(infer_data, dim=0)
    # 执行预测
    cls_prob, bbox_pred, _ = pnet(infer_data)
    cls_prob = torch.squeeze(cls_prob)
    cls_prob = softmax_p(cls_prob)
    bbox_pred = torch.squeeze(bbox_pred)
    return cls_prob.detach().cpu().numpy(), bbox_pred.detach().cpu().numpy()


# 使用RNet模型预测
def predict_rnet(infer_data):
    # 添加待预测的图片
    infer_data = torch.tensor(infer_data, dtype=torch.float32, device=device)
    # 执行预测
    cls_prob, bbox_pred, _ = rnet(infer_data)
    cls_prob = softmax_r(cls_prob)
    return cls_prob.detach().cpu().numpy(), bbox_pred.detach().cpu().numpy()


# 使用ONet模型预测
def predict_onet(infer_data):
    # 添加待预测的图片
    infer_data = torch.tensor(infer_data, dtype=torch.float32, device=device)
    # 执行预测
    cls_prob, bbox_pred, landmark_pred = onet(infer_data)
    cls_prob = softmax_o(cls_prob)
    return cls_prob.detach().cpu().numpy(), bbox_pred.detach().cpu().numpy(), landmark_pred.detach().cpu().numpy()


# 获取PNet网络输出结果
def detect_pnet(im, min_face_size, scale_factor, thresh):
    """通过pnet筛选box和landmark
    参数：
      im:输入图像[h,2,3]
    """
    net_size = 12
    # 人脸和输入图像的比率
    current_scale = float(net_size) / min_face_size
    im_resized = processed_image(im, current_scale)
    _, current_height, current_width = im_resized.shape
    all_boxes = list()
    # 图像金字塔
    while min(current_height, current_width) > net_size:
        # 类别和box
        cls_cls_map, reg = predict_pnet(im_resized)
        boxes = generate_bbox(cls_cls_map[1, :, :], reg, current_scale, thresh)
        current_scale *= scale_factor  # 继续缩小图像做金字塔
        im_resized = processed_image(im, current_scale)
        _, current_height, current_width = im_resized.shape

        if boxes.size == 0:
            continue
        # 非极大值抑制留下重复低的box
        keep = py_nms(boxes[:, :5], 0.5, mode='Union')
        boxes = boxes[keep]
        all_boxes.append(boxes)
    if len(all_boxes) == 0:
        return None
    all_boxes = np.vstack(all_boxes)
    # 将金字塔之后的box也进行非极大值抑制
    keep = py_nms(all_boxes[:, 0:5], 0.7, mode='Union')
    all_boxes = all_boxes[keep]
    # box的长宽
    bbw = all_boxes[:, 2] - all_boxes[:, 0] + 1
    bbh = all_boxes[:, 3] - all_boxes[:, 1] + 1
    # 对应原图的box坐标和分数
    boxes_c = np.vstack([all_boxes[:, 0] + all_boxes[:, 5] * bbw,
                         all_boxes[:, 1] + all_boxes[:, 6] * bbh,
                         all_boxes[:, 2] + all_boxes[:, 7] * bbw,
                         all_boxes[:, 3] + all_boxes[:, 8] * bbh,
                         all_boxes[:, 4]])
    boxes_c = boxes_c.T

    return boxes_c


# 获取RNet网络输出结果
def detect_rnet(im, dets, thresh):
    """通过rent选择box
        参数：
          im：输入图像
          dets:pnet选择的box，是相对原图的绝对坐标
        返回值：
          box绝对坐标
    """
    h, w, c = im.shape
    # 将pnet的box变成包含它的正方形，可以避免信息损失
    dets = convert_to_square(dets)
    dets[:, 0:4] = np.round(dets[:, 0:4])
    # 调整超出图像的box
    [dy, edy, dx, edx, y, ey, x, ex, tmpw, tmph] = pad(dets, w, h)
    delete_size = np.ones_like(tmpw) * 20
    ones = np.ones_like(tmpw)
    zeros = np.zeros_like(tmpw)
    num_boxes = np.sum(np.where((np.minimum(tmpw, tmph) >= delete_size), ones, zeros))
    cropped_ims = np.zeros((num_boxes, 3, 24, 24), dtype=np.float32)
    for i in range(int(num_boxes)):
        # 将pnet生成的box相对与原图进行裁剪，超出部分用0补
        if tmph[i] < 20 or tmpw[i] < 20:
            continue
        tmp = np.zeros((tmph[i], tmpw[i], 3), dtype=np.uint8)
        try:
            tmp[dy[i]:edy[i] + 1, dx[i]:edx[i] + 1, :] = im[y[i]:ey[i] + 1, x[i]:ex[i] + 1, :]
            img = cv2.resize(tmp, (24, 24), interpolation=cv2.INTER_LINEAR)
            img = img.transpose((2, 0, 1))
            img = (img - 127.5) / 128
            cropped_ims[i, :, :, :] = img
        except:
            continue
    cls_scores, reg = predict_rnet(cropped_ims)
    cls_scores = cls_scores[:, 1]
    keep_inds = np.where(cls_scores > thresh)[0]
    if len(keep_inds) > 0:
        boxes = dets[keep_inds]
        boxes[:, 4] = cls_scores[keep_inds]
        reg = reg[keep_inds]
    else:
        return None

    keep = py_nms(boxes, 0.6, mode='Union')
    boxes = boxes[keep]
    # 对pnet截取的图像的坐标进行校准，生成rnet的人脸框对于原图的绝对坐标
    boxes_c = calibrate_box(boxes, reg[keep])
    return boxes_c


# 获取ONet模型预测结果
def detect_onet(im, dets, thresh):
    """将onet的选框继续筛选基本和rnet差不多但多返回了landmark"""
    h, w, c = im.shape
    dets = convert_to_square(dets)
    dets[:, 0:4] = np.round(dets[:, 0:4])
    [dy, edy, dx, edx, y, ey, x, ex, tmpw, tmph] = pad(dets, w, h)
    num_boxes = dets.shape[0]
    cropped_ims = np.zeros((num_boxes, 3, 48, 48), dtype=np.float32)
    for i in range(num_boxes):
        tmp = np.zeros((tmph[i], tmpw[i], 3), dtype=np.uint8)
        tmp[dy[i]:edy[i] + 1, dx[i]:edx[i] + 1, :] = im[y[i]:ey[i] + 1, x[i]:ex[i] + 1, :]
        img = cv2.resize(tmp, (48, 48), interpolation=cv2.INTER_LINEAR)
        img = img.transpose((2, 0, 1))
        img = (img - 127.5) / 128
        cropped_ims[i, :, :, :] = img

    cls_scores, reg, landmark = predict_onet(cropped_ims)

    cls_scores = cls_scores[:, 1]
    keep_inds = np.where(cls_scores > thresh)[0]
    if len(keep_inds) > 0:
        boxes = dets[keep_inds]
        boxes[:, 4] = cls_scores[keep_inds]
        reg = reg[keep_inds]
        landmark = landmark[keep_inds]
    else:
        return None, None

    w = boxes[:, 2] - boxes[:, 0] + 1

    h = boxes[:, 3] - boxes[:, 1] + 1
    landmark[:, 0::2] = (np.tile(w, (5, 1)) * landmark[:, 0::2].T + np.tile(boxes[:, 0], (5, 1)) - 1).T
    landmark[:, 1::2] = (np.tile(h, (5, 1)) * landmark[:, 1::2].T + np.tile(boxes[:, 1], (5, 1)) - 1).T
    boxes_c = calibrate_box(boxes, reg)

    keep = py_nms(boxes_c, 0.6, mode='Minimum')
    boxes_c = boxes_c[keep]
    landmark = landmark[keep]
    return boxes_c, landmark


# 预测图片
def infer_image(im):
    # 调用第一个模型预测
    boxes_c = detect_pnet(im, 20, 0.79, 0.9)
    if boxes_c is None:
        return None, None
    # 调用第二个模型预测
    boxes_c = detect_rnet(im, boxes_c, 0.6)
    if boxes_c is None:
        return None, None
    # 调用第三个模型预测
    boxes_c, landmark = detect_onet(im, boxes_c, 0.7)
    if boxes_c is None:
        return None, None

    return boxes_c, landmark


# 画出人脸框和关键点
def draw_face(img, boxes_c, landmarks):
    for i in range(boxes_c.shape[0]):
        bbox = boxes_c[i, :4]
        score = boxes_c[i, 4]
        corpbbox = [int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])]
        # 画人脸框
        cv2.rectangle(img, (corpbbox[0], corpbbox[1]),
                      (corpbbox[2], corpbbox[3]), (255, 0, 0), 1)
        # 判别为人脸的置信度
        cv2.putText(img, '{:.2f}'.format(score),
                    (corpbbox[0], corpbbox[1] - 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
    # 画关键点
    for i in range(landmarks.shape[0]):
        for j in range(len(landmarks[i]) // 2):
            cv2.circle(img, (int(landmarks[i][2 * j]), int(int(landmarks[i][2 * j + 1]))), 2, (0, 0, 255))
    cv2.imshow('result', img)
    cv2.waitKey(1)

def detect_faces_in_video(video_path, output_path,output_folder):

    cap = cv2.VideoCapture(video_path)

    # 视频编解码参数设置
    fourcc = cv2.VideoWriter_fourcc(*'XVID')
    frame_width = int(cap.get(3))
    frame_height = int(cap.get(4))
    fps = cap.get(cv2.CAP_PROP_FPS)
    # print(fps)
    out = cv2.VideoWriter(output_path, fourcc, 30.0, (frame_width, frame_height))  #30.0指输出视频的帧速度，帧速度越大输出视频的速度越快

    frame_count = 0
    seconds_count = 0
    frame_skip = 90  # 每frame_skip（90）帧处理一次

    # 确保输出文件夹存在
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        # 检测人脸，但只在计数是frame_skip（90）的倍数时执行
        if frame_count % frame_skip == 0:
            boxes_c, landmarks = infer_image(frame)
            if boxes_c is not None:
                # 在图像上绘制人脸框和关键点
                for i in range(boxes_c.shape[0]):
                    bbox = boxes_c[i, :4]
                    corpbbox = [int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])]
                    cv2.rectangle(frame, (corpbbox[0], corpbbox[1]), (corpbbox[2], corpbbox[3]), (255, 0, 0), 1)
                    for j in range(len(landmarks[i]) // 2):
                        cv2.circle(frame, (int(landmarks[i][2 * j]), int(landmarks[i][2 * j + 1])), 2, (0, 0, 255))

                # 保存检测到的人脸图像（每秒保存一次）
                if int(frame_count / fps) > seconds_count:
                    save_detected_faces(frame, boxes_c, landmarks, frame_count, fps)
                    seconds_count += 3

            frame_filename = os.path.join(output_folder, f"frame_{frame_count:04d}.png")
            cv2.imwrite(frame_filename, frame)

        # 将帧写入输出视频
        out.write(frame)
        frame_count += 1

        # # 显示帧
        # cv2.imshow('Video', frame)
        # if cv2.waitKey(1) & 0xFF == ord('q'):
        #     break
    print(f"Finished converting {frame_count} frames.")
    cap.release()
    out.release()
    cv2.destroyAllWindows()

def video_to_frames(video_path, output_folder, frame_skip=1):
    """
    将视频拆分成帧并保存到指定文件夹
    :param video_path: 要拆分的视频路径
    :param output_folder: 保存帧的文件夹路径
    :param frame_skip: 每隔多少帧保存一帧，默认为1（保存所有帧）
    :return: None
    """
    # 确保输出文件夹存在
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    # 打开视频
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print("Error: Cannot open video.")
        return

    # 获取视频的帧数
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    print(f"Converting {total_frames} frames from video {video_path} to images...")

    frame_count = 0
    while True:
        ret, frame = cap.read()
        # 如果没有帧了，退出循环
        if not ret:
            break

        # 根据frame_skip的值决定是否保存帧
        if frame_count % frame_skip == 0:
            # 保存帧到指定文件夹
            frame_filename = os.path.join(output_folder, f"frame_{frame_count:04d}.png")
            cv2.imwrite(frame_filename, frame)

        frame_count += 1

    cap.release()
    print(f"Finished converting {frame_count} frames.")

# def multi_face_Detection(videoUrl ):
#     video_path = 'students-full.mp4'
#     output_path = ('2video.avi')
#     detect_faces_in_video(video_path, output_path)

if __name__ == '__main__':
    video_path = 'dataset/video_test/test.mp4' #视频路径
    output_path = ('dataset/video_test/output_testvideo.avi')     #生成的视频
    # 使用函数提取帧
    output_folder = 'path_to_save_frames'  # 替换为您想要保存帧的文件夹路径
    detect_faces_in_video(video_path, output_path,output_folder)

    # video_to_frames(output_path, 'output_frames_folder', frame_skip=300) #将视频拆分成帧并保存到指定文件夹 frame_skip: 每隔多少帧保存一帧，默认为1（保存所有帧）

