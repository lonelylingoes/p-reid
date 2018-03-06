#-*- coding:utf-8 -*-
#===================================
# deploy program
#===================================
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys
sys.path.append('../')


import os
import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable
from model.model import Model
import utils.common_utils as common_utils 
import utils.model_utils as model_utils
from utils.model_utils import transer_var_tensor
import  model.loss as loss
from utils.common_utils import measure_time
from utils.re_ranking import re_ranking



def low_memory_local_dist(x, y):
    '''
    Args:
        x: numpy array, with shape []
        y: numpy array, with shape []
    Returns:
        dist: numpy array, with shape []
    '''
    with measure_time('Computing local distance...'):
        x_num_splits = int(len(x) / 200) + 1
        y_num_splits = int(len(y) / 200) + 1
        z = loss.low_memory_matrix_op(
                loss.local_dist_np, x, y, 0, 0, x_num_splits, y_num_splits, verbose=True)
    return z


class ReId(object):
    '''
    the class is created for deloy purpose.
    '''
    
    def __init__(self, 
                model_path,
                device_id = 0):
        '''
        args:
            model_path: the model file path
            image_path: the image file path
        '''
        class Config(object):
            pass
        cfg = Config
        cfg.model_weight_file = ''
        cfg.ckpt_file = model_path
        self.device_id = device_id
        torch.cuda.set_device(device_id)
        # create model
        self.model = Model(local_conv_out_channels=128, pretrained = False)
        # load model param
        self.model = model_utils.load_test_model(self.model, cfg)
        # after load model, parallel the model
        if torch.cuda.device_count() > 1:
            self.model = nn.DataParallel(self.model, device_ids=[device_id])
        if torch.cuda.is_available():
            self.model.cuda()
    

    def __parse_image_name__(self, image_name):
        '''
        parse the image name to mark, person id, camera id, scene id, 
        args:
            image_name: the name of the image
        '''
        mark = int(image_name[0])
        person_id = int(image_name[2:6])
        camera_id = int(image_name[8])
        scene_id = int(image_name[10])
        return mark, person_id, camera_id, scene_id


    def __get_threshold__(self,
                         to_re_rank,
                        use_local_distance,
                        normalize_feature):
        if to_re_rank:
            threshold = 0.3
        elif normalize_feature:
            if use_local_distance:
                threshold = 6
            else:
                threshold = 1.5   
        else:
            if use_local_distance:
                threshold = 20
            else:
                threshold = 24
        return threshold


    def decide(self, 
                images_path,
                to_re_rank=True,
                use_local_distance=False,
                normalize_feature = False):
        '''
        get the result
        args:
            images_path: the path of images
            to_re_rank: whether use re_rank
            use_local_distance: whether use local distance
            normalize_feature: whether normalize the features
        returns:
            found_ids:

        '''
        # get images infomation
        images, marks, person_ids, camera_ids, scene_ids = self.__get_images_info__(images_path)
        # get query and gallery indicate
        q_inds = (marks == 0)
        g_inds = (marks == 1)
        # get the distance matrix
        dist_mat= self.__compute_distance_mat__(images, marks, q_inds, g_inds,
                                                to_re_rank, use_local_distance, normalize_feature)
        # query_ids = person_ids[q_inds]
        gallery_ids = person_ids[g_inds]
        # if found, fill the id else fill -1
        found_ids = []
        # the numberof query is m, the number of gallery is n
        m, n = dist_mat.shape
        # the threshold decides whether same
        threshold = self.__get_threshold__(to_re_rank, use_local_distance, normalize_feature)
        # sort and find correct matches
        indexs = np.argmin(dist_mat, axis=1)
        sorted_dis = np.sort(dist_mat, axis =1)

        # judge for every query
        for i in range(m):
            # for query i, in the gallery set, the shortest distance is less than the threshold
            if sorted_dis[i][0] < threshold:
                found_ids.append(gallery_ids[indexs][i])
            else:
                found_ids.append(-1)

        found_ids = self.__remvoe_overlap__(sorted_dis[:,0], found_ids)
        return found_ids


    def __remvoe_overlap__(self, sorted_dis_vect, found_ids):
        '''
        for every query remove the overlap gallery id by compare the distances
        args:
            sorted_dis_vect: for very for every query the shortest distance vector,numpy array
            found_ids: for very query denote whether found, list     
        '''
        found_array = np.array(found_ids)
        # for all item in gallery_ids
        for ids in found_ids:
            if ids == -1:
                continue
            index = np.argwhere(found_array == ids)
            if len(index) > 1:#find overlap
                cut_vect = sorted_dis_vect[index]
                shortest_dis_index = np.argmin(cut_vect)
                for i in range(len(index)):
                    if i == shortest_dis_index:
                        continue
                    found_ids[index[i][0]] = -1
        return found_ids


    def __get_images_info__(self, images_path):
        '''
        read detected images and base images from 'images_path',
        and return images arrary, marks, person ids, camera ids, scene ids, 
        '''
        images=[]
        marks=[]
        person_ids=[]
        camera_ids=[]
        scene_ids=[]
        files = os.listdir(images_path)
        files.sort()
        for file in files:
            mark, person_id, camera_id, scene_id = self.__parse_image_name__(file)
            images.append(common_utils.pre_process_im(os.path.join(images_path, file), (256, 128)))
            marks.append(mark)
            person_ids.append(person_id)
            camera_ids.append(camera_id)
            scene_ids.append(scene_id)
        images = np.array(images)
        marks = np.array(marks)
        person_ids = np.array(person_ids)
        camera_ids = np.array(camera_ids)
        scene_ids = np.array(scene_ids)

        return images, marks, person_ids, camera_ids, scene_ids



    def __compute_distance_mat__(self, 
                            images,
                            marks,
                            q_inds,
                            g_inds,
                            to_re_rank,
                            use_local_distance,
                            normalize_feature):
        '''
        compute distance mat
        args:
            images: the numpy arrary of images
            marks: the numpy arrary of marks denote query images or gallery images
            q_inds: query images indicates
            g_inds: gallery images indicates
            to_re_rank: whether use re_rank
            use_local_distance: whether use local distance
            normalize_feature: whether normalize the features
        returns:
            the finnal distance matrix
        '''
        with measure_time('Extrating feature...'):
            ims_var = Variable(transer_var_tensor(torch.from_numpy(images), self.device_id).float(), volatile=True)
            global_feats, local_feats = self.model(ims_var)[:2]
            global_feats = global_feats.data.cpu().numpy()
            local_feats = local_feats.data.cpu().numpy()

        if normalize_feature:
            global_feats = loss.normalize_np(global_feats, axis=1)
            local_feats = loss.normalize_np(local_feats, axis=-1)

        # Global Distance 
        with measure_time('Computing global distance...'):
            # query-gallery distance using global distance
            global_q_g_dist = loss.compute_dist_np(
                global_feats[q_inds], global_feats[g_inds], type='euclidean')

        if to_re_rank:
            with measure_time('Re-ranking...'):
                # query-query distance using global distance
                global_q_q_dist = loss.compute_dist_np(
                    global_feats[q_inds], global_feats[q_inds], type='euclidean')
                # gallery-gallery distance using global distance
                global_g_g_dist = loss.compute_dist_np(
                    global_feats[g_inds], global_feats[g_inds], type='euclidean')
                # re-ranked global query-gallery distance
                re_global_q_g_dist = re_ranking(
                    global_q_g_dist, global_q_q_dist, global_g_g_dist)

        # Local Distance 
        if use_local_distance:
            with measure_time('Computing local distance...'):
                # query-gallery distance using local distance
                local_q_g_dist = low_memory_local_dist(
                    local_feats[q_inds], local_feats[g_inds])
            if to_re_rank:
                with measure_time('Re-ranking...'):
                    # query-query distance using local distance
                    local_q_q_dist = low_memory_local_dist(
                        local_feats[q_inds], local_feats[q_inds])
                    # gallery-gallery distance using local distance
                    local_g_g_dist = low_memory_local_dist(
                        local_feats[g_inds], local_feats[g_inds])

            # Global+Local Distance 
            global_local_q_g_dist = global_q_g_dist + local_q_g_dist
            if to_re_rank:
                with measure_time('Re-ranking...'):
                    global_local_q_q_dist = global_q_q_dist + local_q_q_dist
                    global_local_g_g_dist = global_g_g_dist + local_g_g_dist
                    re_global_local_q_g_dist = re_ranking(
                        global_local_q_g_dist, global_local_q_q_dist, global_local_g_g_dist)

        # return distance
        if use_local_distance:
            if to_re_rank:
                return re_global_local_q_g_dist
            else:
                return global_local_q_g_dist
        else:
            if to_re_rank:
                return re_global_q_g_dist
            else:
                return global_q_g_dist



def main():
    reId = ReId('/data/chensijing/AlignedReID/ckpt_dir/ckpt_path')
    for i in range(10):
        found_ids = reId.decide('/home/ubun-titan/Debug/image_dir1')
    print(found_ids)

if __name__ == '__main__':
    main()