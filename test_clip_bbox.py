#!/usr/bin/env python3

import numpy as np
import torch

import argparse
import logging
import sys
import subprocess
import os
import time

# Parse arguments
parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument("--run-name", required=True)
parser.add_argument("--num-segs", required=True, type=int)
parser.add_argument("--job-start", required=True, type=int)
parser.add_argument("--k", required=True, type=int)
args = parser.parse_args()

print("Torch version:", torch.__version__)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(device)

MODELS = {
    "RN50": "https://openaipublic.azureedge.net/clip/models/afeb0e10f9e5a86da6080e35cf09123aca3b358a0c3e3b6c78a7b63bc04b6762/RN50.pt",
    "RN101": "https://openaipublic.azureedge.net/clip/models/8fa8567bab74a42d41c5915025a8e4538c3bdbe8804a470a72f30b0d94fab599/RN101.pt",
    "RN50x4": "https://openaipublic.azureedge.net/clip/models/7e526bd135e493cef0776de27d5f42653e6b4c8bf9e0f653bb11773263205fdd/RN50x4.pt",
    "ViT-B/32": "https://openaipublic.azureedge.net/clip/models/40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af/ViT-B-32.pt",    
}

input_resolution = (720,1280)
model_old = torch.jit.load("/dvmm-filer2/users/grace/clip/model_rn50.pt").cuda().eval()
context_length = model_old.context_length.item()
vocab_size = model_old.vocab_size.item()

print("Model parameters:", f"{np.sum([int(np.prod(p.shape)) for p in model_old.parameters()]):,}")
print("Input resolution:", input_resolution)
print("Context length:", context_length)
print("Vocab size:", vocab_size)

import sys
sys.path.append('/dvmm-filer2/users/grace/CLIP/clip')
from model import *
from newpad import *

model_old.state_dict()['input_resolution'] = torch.tensor([720,1280])
print("Model's state_dict:")
for param_tensor in model_old.state_dict():
    # print(param_tensor + '            ' + str(model_old.state_dict()[param_tensor].size()))
    model_old.state_dict()[param_tensor] = model_old.state_dict()[param_tensor].cuda()
    
# parallelize
model_modded = build_model(model_old.state_dict())
# model_modded = MyDataParallel(model_modded, device_ids=[0,1,2,3])
print("check model cuda: {}".format(next(model_modded.parameters()).is_cuda))
model_modded.to(device)
print('device count: {}'.format(torch.cuda.device_count()))
# CLIP IMAGE and TEXT PREPROCESSING
from torchvision.transforms import Compose, Resize, CenterCrop, ToTensor, Normalize
from PIL import Image

preprocess = Compose([
    # NewPad(),
    Resize(input_resolution, interpolation=Image.BICUBIC),
    #CenterCrop(input_resolution),
    ToTensor()
])

image_mean = torch.tensor([0.48145466, 0.4578275, 0.40821073]).cuda()
image_std = torch.tensor([0.26862954, 0.26130258, 0.27577711]).cuda()


import gzip
import html
from functools import lru_cache

import ftfy
import regex as re

# os.system("wget https://openaipublic.azureedge.net/clip/bpe_simple_vocab_16e6.txt.gz -O bpe_simple_vocab_16e6.txt.gz")

@lru_cache()
def bytes_to_unicode():
    """
    Returns list of utf-8 byte and a corresponding list of unicode strings.
    The reversible bpe codes work on unicode strings.
    This means you need a large # of unicode characters in your vocab if you want to avoid UNKs.
    When you're at something like a 10B token dataset you end up needing around 5K for decent coverage.
    This is a signficant percentage of your normal, say, 32K bpe vocab.
    To avoid that, we want lookup tables between utf-8 bytes and unicode strings.
    And avoids mapping to whitespace/control characters the bpe code barfs on.
    """
    bs = list(range(ord("!"), ord("~")+1))+list(range(ord("¡"), ord("¬")+1))+list(range(ord("®"), ord("ÿ")+1))
    cs = bs[:]
    n = 0
    for b in range(2**8):
        if b not in bs:
            bs.append(b)
            cs.append(2**8+n)
            n += 1
    cs = [chr(n) for n in cs]
    return dict(zip(bs, cs))


def get_pairs(word):
    """Return set of symbol pairs in a word.
    Word is represented as tuple of symbols (symbols being variable-length strings).
    """
    pairs = set()
    prev_char = word[0]
    for char in word[1:]:
        pairs.add((prev_char, char))
        prev_char = char
    return pairs


def basic_clean(text):
    text = ftfy.fix_text(text)
    text = html.unescape(html.unescape(text))
    return text.strip()


def whitespace_clean(text):
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    return text


class SimpleTokenizer(object):
    def __init__(self, bpe_path: str = "bpe_simple_vocab_16e6.txt.gz"):
        self.byte_encoder = bytes_to_unicode()
        self.byte_decoder = {v: k for k, v in self.byte_encoder.items()}
        merges = gzip.open(bpe_path).read().decode("utf-8").split('\n')
        merges = merges[1:49152-256-2+1]
        merges = [tuple(merge.split()) for merge in merges]
        vocab = list(bytes_to_unicode().values())
        vocab = vocab + [v+'</w>' for v in vocab]
        for merge in merges:
            vocab.append(''.join(merge))
        vocab.extend(['<|startoftext|>', '<|endoftext|>'])
        self.encoder = dict(zip(vocab, range(len(vocab))))
        self.decoder = {v: k for k, v in self.encoder.items()}
        self.bpe_ranks = dict(zip(merges, range(len(merges))))
        self.cache = {'<|startoftext|>': '<|startoftext|>', '<|endoftext|>': '<|endoftext|>'}
        self.pat = re.compile(r"""<\|startoftext\|>|<\|endoftext\|>|'s|'t|'re|'ve|'m|'ll|'d|[\p{L}]+|[\p{N}]|[^\s\p{L}\p{N}]+""", re.IGNORECASE)

    def bpe(self, token):
        if token in self.cache:
            return self.cache[token]
        word = tuple(token[:-1]) + ( token[-1] + '</w>',)
        pairs = get_pairs(word)

        if not pairs:
            return token+'</w>'

        while True:
            bigram = min(pairs, key = lambda pair: self.bpe_ranks.get(pair, float('inf')))
            if bigram not in self.bpe_ranks:
                break
            first, second = bigram
            new_word = []
            i = 0
            while i < len(word):
                try:
                    j = word.index(first, i)
                    new_word.extend(word[i:j])
                    i = j
                except:
                    new_word.extend(word[i:])
                    break

                if word[i] == first and i < len(word)-1 and word[i+1] == second:
                    new_word.append(first+second)
                    i += 2
                else:
                    new_word.append(word[i])
                    i += 1
            new_word = tuple(new_word)
            word = new_word
            if len(word) == 1:
                break
            else:
                pairs = get_pairs(word)
        word = ' '.join(word)
        self.cache[token] = word
        return word

    def encode(self, text):
        bpe_tokens = []
        text = whitespace_clean(basic_clean(text)).lower()
        for token in re.findall(self.pat, text):
            token = ''.join(self.byte_encoder[b] for b in token.encode('utf-8'))
            bpe_tokens.extend(self.encoder[bpe_token] for bpe_token in self.bpe(token).split(' '))
        return bpe_tokens

    def decode(self, tokens):
        text = ''.join([self.decoder[token] for token in tokens])
        text = bytearray([self.byte_decoder[c] for c in text]).decode('utf-8', errors="replace").replace('</w>', ' ')
        return text

import os
import skimage
import IPython.display
import matplotlib.pyplot as plt
from PIL import Image

from collections import OrderedDict

print(skimage.__version__)

# images in skimage to use and their textual descriptions
descriptions = {
    "page": "a page of text about segmentation",
    "chelsea": "a facial photo of a tabby cat",
    "astronaut": "a portrait of an astronaut with the American flag",
    "rocket": "a rocket standing on a launchpad",
    "motorcycle_right": "a red motorcycle standing in a garage",
    "camera": "a person looking at a camera on a tripod",
    "horse": "a black-and-white silhouette of a horse", 
    "coffee": "a cup of coffee on a saucer"
}

import math
import torch.nn.functional as F

def img_fts_to_heatmap(img_fts, txt_fts):
    """
    img_fts: list of image features with no global avg pooling layer, shape=(img_dim x img_dim, batch_size, features)
    Return: list of heatmaps
    """

    # dot product with normalization
    img_norm = img_fts / img_fts.norm(dim=-1, keepdim=True)
    txt_norm = txt_fts / txt_fts.norm(dim=-1, keepdim=True)

    batch_size = len(txt_norm)
    print(batch_size)
    #img_dim = int(math.sqrt(img_fts.size()[0]))
    #img_norm = F.interpolate(img_norm.permute(2,1,0), size=int(input_resolution[0]*input_resolution[1]/64), mode='nearest').permute(2,1,0)
    img_reshape = torch.reshape(img_norm, (22, 40, batch_size, img_fts.size()[2]))
    print(img_reshape.shape)
    print(txt_norm.shape)
    
    img_reshape = img_reshape.cpu()
    
    heatmap_norm = img_reshape.cpu().numpy() @ txt_norm.cpu().numpy().T

    #heatmap_list = [-1.0 * heatmap_norm[:,:,h,h] + heatmap_norm[:,:,h,h].min() for h in range(batch_size)]
    
    # my loop
    heatmap_list = []
    for h in range(batch_size):
        maxi = heatmap_norm[:,:,h,h].max()
        print(maxi)
        hm = -1.0 * heatmap_norm[:,:,h,h] + heatmap_norm[:,:,h,h].max()
        # hm = hm / np.linalg.norm(hm)
        heatmap_list.append(hm)

    return heatmap_list

# GET BBOX FROM HEATMAPS

#bbox generation config
rel_peak_thr = .6
rel_rel_thr = .3
ioa_thr = .6
topk_boxes = args.k
from skimage.feature import peak_local_max
def heat2bbox(heat_map, original_image_shape):
    
    h, w = heat_map.shape
    
    bounding_boxes = []

    heat_map = heat_map - np.min(heat_map)
    heat_map = heat_map / np.max(heat_map)

    bboxes = []
    box_scores = []

    peak_coords = peak_local_max(heat_map, exclude_border=False, threshold_rel=rel_peak_thr) # find local peaks of heat map

    heat_resized = cv2.resize(heat_map, (original_image_shape[1],original_image_shape[0]))  ## resize heat map to original image shape
    peak_coords_resized = ((peak_coords + 0.5) * 
                           np.asarray([original_image_shape]) / 
                           np.asarray([[h, w]])
                          ).astype('int32')

    for pk_coord in peak_coords_resized:
        pk_value = heat_resized[tuple(pk_coord)]
        mask = heat_resized > pk_value * rel_rel_thr
        labeled, n = ndi.label(mask) 
        l = labeled[tuple(pk_coord)]
        yy, xx = np.where(labeled == l)
        min_x = np.min(xx)
        min_y = np.min(yy)
        max_x = np.max(xx)
        max_y = np.max(yy)
        bboxes.append((min_x, min_y, max_x, max_y))
        box_scores.append(pk_value) # you can change to pk_value * probability of sentence matching image or etc.


    ## Merging boxes that overlap too much
    box_idx = np.argsort(-np.asarray(box_scores))
    box_idx = box_idx[:min(topk_boxes, len(box_scores))]
    bboxes = [bboxes[i] for i in box_idx]
    box_scores = [box_scores[i] for i in box_idx]

    to_remove = []
    for iii in range(len(bboxes)):
        for iiii in range(iii):
            if iiii in to_remove:
                continue
            b1 = bboxes[iii]
            b2 = bboxes[iiii]
            isec = max(min(b1[2], b2[2]) - max(b1[0], b2[0]), 0) * max(min(b1[3], b2[3]) - max(b1[1], b2[1]), 0)
            ioa1 = isec / ((b1[2] - b1[0]) * (b1[3] - b1[1]))
            ioa2 = isec / ((b2[2] - b2[0]) * (b2[3] - b2[1]))
            if ioa1 > ioa_thr and ioa1 == ioa2:
                to_remove.append(iii)
            elif ioa1 > ioa_thr and ioa1 >= ioa2:
                to_remove.append(iii)
            elif ioa2 > ioa_thr and ioa2 >= ioa1:
                to_remove.append(iiii)

    for i in range(len(bboxes)): 
        if i not in to_remove:
            bounding_boxes.append({
                'score': box_scores[i],
                'bbox': bboxes[i],
                'bbox_normalized': np.asarray([
                    bboxes[i][0] / heat_resized.shape[1],
                    bboxes[i][1] / heat_resized.shape[0],
                    bboxes[i][2] / heat_resized.shape[1],
                    bboxes[i][3] / heat_resized.shape[0],
                ]),
            })
    
    return bounding_boxes

def img_heat_bbox_disp(image, heat_map, title='', en_name='', alpha=0.6, cmap='viridis', cbar='False', dot_max=False, pred_bboxes=[], gt_bboxes=[], order=None, show=True):
    thr_hit = 1 #a bbox is acceptable if hit point is in middle 85% of bbox area
    thr_fit = .60 #the biggest acceptable bbox should not exceed 60% of the image
    H, W = image.shape[0:2]
    # resize heat map
    heat_map_resized = cv2.resize(heat_map, (W,H))
    print(heat_map_resized.shape)
    # display
    fig = plt.figure(figsize=(15,15))
    fig.suptitle(title, size=15)
    ax = plt.subplot(1,3,1)
    plt.imshow(image)
    if dot_max:
        max_loc = np.unravel_index(np.argmax(heat_map_resized, axis=None), heat_map_resized.shape)
        plt.scatter(x=max_loc[1], y=max_loc[0], edgecolor='w', linewidth=3)
    
    if len(bboxes)>0: #it gets normalized bbox
        if order==None:
            order='xxyy'
        
        for i in range(len(bboxes)):
            bbox_norm = bboxes[i]['bbox_normalized']
            if order=='xxyy':
                x_min,x_max,y_min,y_max = int(bbox_norm[0]*W),int(bbox_norm[1]*W),int(bbox_norm[2]*H),int(bbox_norm[3]*H)
            elif order=='xyxy':
                x_min,x_max,y_min,y_max = int(bbox_norm[0]*W),int(bbox_norm[2]*W),int(bbox_norm[1]*H),int(bbox_norm[3]*H)
            x_length,y_length = x_max-x_min,y_max-y_min
            print(x_min, y_min, x_length, y_length)
            box = plt.Rectangle((x_min,y_min),x_length,y_length, edgecolor='r', linewidth=3, fill=False)
            plt.gca().add_patch(box)
            if en_name!='':
                ax.text(x_min+.5*x_length,y_min+10, en_name,
                verticalalignment='center', horizontalalignment='center',
                #transform=ax.transAxes,
                color='white', fontsize=15)
                #an = ax.annotate(en_name, xy=(x_min,y_min), xycoords="data", va="center", ha="center", bbox=dict(boxstyle="round", fc="w"))
                #plt.gca().add_patch(an)
            
    plt.imshow(heat_map_resized, alpha=alpha, cmap=cmap)
    
    #plt.figure(2, figsize=(6, 6))
    plt.subplot(1,3,2)
    plt.imshow(image)
    #plt.figure(3, figsize=(6, 6))
    plt.subplot(1,3,3)
    plt.imshow(heat_map_resized)
    plt.colorbar()
    fig.tight_layout()
    fig.subplots_adjust(top=.85)
    
    if show:
        plt.show()
    else:
        plt.close()
    
    return fig

import cv2
from scipy import ndimage as ndi

"""
for h in range(len(heatmap_list)):

    title = "Image " + str(h) + ", Text: \"" + texts[h] + "\""
    heat = heatmap_list[h]
    print(heat.shape)
    bboxes = heat2bbox(heat, input_resolution)
    print(bboxes)
    img_heat_bbox_disp(images[h].permute(1, 2, 0), heat, title=title, bboxes=bboxes, order='xyxy') 
"""

def intersect(box_a, box_b):
    """ We resize both tensors to [A,B,2] without new malloc:
    [A,2] -> [A,1,2] -> [A,B,2]
    [B,2] -> [1,B,2] -> [A,B,2]
    Then we compute the area of intersect between box_a and box_b.
    Args:
      box_a: (tensor) bounding boxes, Shape: [A,4].
      box_b: (tensor) bounding boxes, Shape: [B,4].
    Return:
      (tensor) intersection area, Shape: [A,B].
    """
    A = box_a.size(0)
    B = box_b.size(0)
    max_xy = torch.min(box_a[:, 2:].unsqueeze(1).expand(A, B, 2),
                       box_b[:, 2:].unsqueeze(0).expand(A, B, 2))
    min_xy = torch.max(box_a[:, :2].unsqueeze(1).expand(A, B, 2),
                       box_b[:, :2].unsqueeze(0).expand(A, B, 2))

    inter = torch.clamp((max_xy - min_xy), min=0)
    return inter[:, :, 0] * inter[:, :, 1]

def jaccard(gt_bbox, bbox_list):
    """Compute the jaccard overlap of two sets of boxes.  The jaccard overlap
    is simply the intersection over union of two boxes.  Here we operate on
    ground truth boxes and default boxes.
    E.g.:
        A ∩ B / A U B = A ∩ B / (area(A) + area(B) - A ∩ B)
    Args:
        box_a: (tensor) Ground truth bounding boxes, Shape: [num_objects,4]
        box_b: (tensor) Prior boxes from priorbox layers, Shape: [num_priors,4]
    Return:
        jaccard overlap: (tensor) Shape: [box_a.size(0), box_b.size(0)]
    """ 
    
    inter = intersect(gt_bbox, bbox_list[0])
    
    for i in range(1, len(bbox_list)):
        b = bbox_list[i]
        inter += intersect(gt_bbox, bbox_list[i])
 
    area_a = ((gt_bbox[:, 2]-gt_bbox[:, 0]) *
              (gt_bbox[:, 3]-gt_bbox[:, 1])).unsqueeze(1).expand_as(inter)  # [A,B]
    
    area_b = ((bbox_list[0][:, 2]-bbox_list[0][:, 0]) *
              (bbox_list[0][:, 3]-bbox_list[0][:, 1])).unsqueeze(0).expand_as(inter)
    
    for i in range(1, len(bbox_list)):
        b = bbox_list[i]
        area_b += ((b[:, 2]-b[:, 0]) *
                  (b[:, 3]-b[:, 1])).unsqueeze(0).expand_as(inter)
        
    union = area_a + area_b - inter
    return inter / union  # [A,B]

def mod_jaccard(gt_bbox, bbox_list):
    """Compute the jaccard overlap of two sets of boxes.  The jaccard overlap
    is simply the intersection over union of two boxes.  Here we operate on
    ground truth boxes and default boxes.
    E.g.:
        A ∩ B / A U B = A ∩ B / (area(A) + area(B) - A ∩ B)
    Args:
        box_a: (tensor) Ground truth bounding boxes, Shape: [num_objects,4]
        box_b: (tensor) Prior boxes from priorbox layers, Shape: [num_priors,4]
    Return:
        jaccard overlap: (tensor) Shape: [box_a.size(0), box_b.size(0)]
    """ 
    
    inter = intersect(gt_bbox, bbox_list[0])
    
    for i in range(1, len(bbox_list)):
        b = bbox_list[i]
        inter += intersect(gt_bbox, bbox_list[i])
 
    area_a = ((gt_bbox[:, 2]-gt_bbox[:, 0]) *
              (gt_bbox[:, 3]-gt_bbox[:, 1])).unsqueeze(1).expand_as(inter)  # [A,B]
    
    area_b = ((bbox_list[0][:, 2]-bbox_list[0][:, 0]) *
              (bbox_list[0][:, 3]-bbox_list[0][:, 1])).unsqueeze(0).expand_as(inter)
    
    for i in range(1, len(bbox_list)):
        b = bbox_list[i]
        area_b += ((b[:, 2]-b[:, 0]) *
                  (b[:, 3]-b[:, 1])).unsqueeze(0).expand_as(inter)
        
    return inter / area_b  # [A,B]

# ACTIVITYNET TEST
from PIL import Image, ImageDraw
import json
import pprint
bb_list = []
cls_list = []
img_list = []
img_path_list = []
img_dim_list = []

json_start_idx = args.job_start
json_end_idx = json_start_idx + args.num_segs
start_time = time.time()

f = open("/dvmm-filer2/users/shyam/video-event-extraction/train_final_single_verbs.json", "r") 
frame_dict = json.load(f)
seg_list = list(frame_dict.keys())[json_start_idx : json_end_idx]

result_dict = {}

for seg in seg_list:
    result_dict[seg] = frame_dict[seg]
    verb = frame_dict[seg]['verb'][0] # assuming single verbs only
    arg_list = list(frame_dict[seg]['arg2vid'][verb].keys())

    for arg in arg_list:

        arg = list(frame_dict[seg]['arg2vid'][verb].keys())[0]
        cls = frame_dict[seg]['frames'][verb][arg]

        # replace all hyphens with spaces
        if '-' in cls:
            cls = cls.replace('-', ' ')

        box_list = frame_dict[seg]['arg2vid'][verb][arg]

        for box_idx in box_list:               
            frame_idx = frame_dict[seg]['bb2frames'][box_idx]
            img_path = "/dvmm-filer2/users/shyam/video-event-extraction/data/train/" + seg +"/" + str(frame_idx) + ".jpg"
            
            try:
                x = Image.open(img_path).convert("RGB")
            except Exception as e:
                # if image does not exist, remove from result dict
                # TODO: does this work on out of scope variables?
                # Do empty arg lists need to be handled differently?
                result_dict[seg]['arg2vid'][verb][arg].remove(box_idx)
                result_dict[seg]['bb2frames'].remove(frame_idx)

                print(e)
                continue

            test_image = preprocess(x)

            img_path_list.append(img_path)
            img_list.append(test_image)
            bb_list.append(frame_dict[seg]['bb'][box_idx])
            cls_list.append(cls)

            # inference for bbox batch of size 1
            image_input = torch.tensor(np.stack(img_list)).cuda()
            image_input -= image_mean[:, None, None]
            image_input /= image_std[:, None, None]

            tokenizer = SimpleTokenizer()
            text_tokens = [tokenizer.encode(desc) for desc in cls_list]

            text_input = torch.zeros(len(text_tokens), model_modded.context_length, dtype=torch.long)
            sot_token = tokenizer.encoder['<|startoftext|>']
            eot_token = tokenizer.encoder['<|endoftext|>']

            for i, tokens in enumerate(text_tokens):
                tokens = [sot_token] + tokens + [eot_token]
                text_input[i, :len(tokens)] = torch.tensor(tokens)

            text_input = text_input.cuda()

            with torch.no_grad():
                image_features = model_modded.encode_image(image_input).float()
                text_features = model_modded.encode_text(text_input).float()

            # get bbox results
            img_fts = image_features[1:]

            heatmap_list = img_fts_to_heatmap(img_fts, text_features)
            pred_bboxes = []
            for h in range(len(heatmap_list)):
                title = "Image " + str(h) + ", Text: \"" + cls_list[h] + "\""
                heat = heatmap_list[h]
                bboxes = heat2bbox(heat, input_resolution)
                pred_bboxes.append([torch.tensor(b['bbox_normalized']).unsqueeze(0) for b in bboxes])
                #img_heat_bbox_disp(img_list[h].permute(1, 2, 0), heat, title=title, bboxes=bboxes, order='xyxy') 

            # resize pred_bboxes to gt image width and height
            W = 720
            H = 405
            for i in range(len(pred_bboxes)):
                for j in range(len(pred_bboxes[i])):
                    
                    bbox_norm = pred_bboxes[i][j][0]
                    pred_bboxes[i][j][0] = torch.tensor([int(bbox_norm[0]*W),int(bbox_norm[1]*H),int(bbox_norm[2]*W),int(bbox_norm[3]*H),])

            bb_list = [torch.tensor(b).unsqueeze(0) for b in bb_list]

            # Calculate IoUs
            iou = jaccard(bb_list[0], pred_bboxes[0]).squeeze(0)
            mod_iou = mod_jaccard(bb_list[0], pred_bboxes[0]).squeeze(0)

            # Write results to dict 
            # TODO: how to handle when len(pred_bboxes) != len(gt_bboxes) ?
            for j in range(len(pred_bboxes[0])):
                pred_bboxes[0][j] = pred_bboxes[0][j].squeeze(0).tolist()

            result_dict[seg]['clip_pred_bbox_list'] = pred_bboxes[0]

            if "clip_iou_list" not in result_dict[seg]:
                result_dict[seg]['clip_iou_list'] = []
            result_dict[seg]['clip_iou_list'].append(iou)

            if "clip_mod_iou_list" not in result_dict[seg]:
                result_dict[seg]['clip_mod_iou_list'] = []
            result_dict[seg]['clip_mod_iou_list'].append(mod_iou)

# write result dict to json
out_path = "/dvmm-filer2/users/grace/CLIP/{}_k{}_{}-{}.json".format(args.run_name, args.k, args.job_start, json_end_idx)
outfile = open(out_path, "w") 
json_string = json.dumps(result_dict)
outfile.write(json_string)

load_time = time.time()
print('job complete: {} seconds'.format(load_time - start_time))

""""
plt.figure(figsize=(16, 5))
for i in range(10):

    with Image.open(img_path_list[i]) as im:
        im1 = im.convert('RGB')
        im1 = np.asarray(im).astype(np.float32)
        im1 = im1.resize(())
        print('-------------------------------------------')
        print(cls_list[i])
        print('iou: {}'.format(iou_list[i].item()))
        print('modified iou: {}'.format(mod_iou_list[i].item()))
        draw = ImageDraw.Draw(im)

        draw.rectangle(bb_list[i].squeeze(0).tolist(), outline='blue', width=2)
        for j in range(len(pred_bboxes[i])):
            draw.rectangle(pred_bboxes[i][j].squeeze(0).tolist(), outline='red', width=2)
        im.show()
"""
# VIDSITU TEST

# close all files
f.close()
outfile.close()