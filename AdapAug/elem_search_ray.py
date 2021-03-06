import copy
import os
import sys
import time
from collections import OrderedDict, defaultdict, Counter

import torch
from torch.distributions import Categorical
import numpy as np
import ray
from ray import tune
from ray.tune.schedulers import AsyncHyperBandScheduler
from ray.tune.suggest.hyperopt import HyperOptSearch
from ray.tune.suggest import ConcurrencyLimiter
from ray.tune import register_trainable, run_experiments, run, Experiment, CLIReporter

from tqdm import tqdm

from pathlib import Path
lib_dir = (Path("__file__").parent).resolve()
if str(lib_dir) not in sys.path: sys.path.insert(0, str(lib_dir))
from AdapAug.common import get_logger, add_filehandler
from AdapAug.metrics import Accumulator, accuracy
from AdapAug.networks import get_model, num_class
from AdapAug.train import train_and_eval
from theconf import Config as C, ConfigArgumentParser
from AdapAug.controller import Controller
from AdapAug.train_ctl import train_controller, train_controller2, train_controller3
import csv, random
import warnings

logger = get_logger('Adap AutoAugment')

def _get_path(dataset, model, tag, basemodel=True):
    base_path = "models" if basemodel else f"models/{C.get()['exp_name']}"
    base_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), base_path)
    os.makedirs(base_path, exist_ok=True)
    return os.path.join(base_path, '%s_%s_%s.model' % (dataset, model, tag))     # TODO

def train_ctl_wrapper(config, augment, reporter):
    warnings.filterwarnings("ignore")
    C.get()
    C.get().conf = config
    if augment['version'] == 2:
        train_ctl = train_controller2
        C.get()['exp_name'] = f"{augment['dataset']}_v{augment['version']}_{augment['mode']}_a{int(augment['aff_step'])}_d{int(augment['div_step'])}_cagg{int(augment['ctl_num_aggre'])}_id{augment['cv_id']}"
    elif augment['version'] == 3:
        train_ctl = train_controller3
        if 'div_w' not in augment:
            augment['div_w'] = 1-augment['aff_w']
        C.get()['exp_name'] = f"{augment['dataset']}_{C.get()['model']['type']}_v{augment['version']}_{augment['mode']}_np{augment['num_policy']}_aw{augment['aff_w']:.0e}_dw{augment['div_w']:.0e}_rt{augment['reward_type']}_{augment['cv_id']}"
        # C.get()['exp_name'] = f"{augment['dataset']}_v{augment['version']}_{augment['mode']}_np{augment['num_policy']}_aw{augment['aff_w']:.3e}_rt{augment['reward_type']}_ew{augment['ctl_entropy_w']:e}_{augment['cv_id']}"
    else:
        train_ctl = train_controller

    base_path = os.path.join(augment['base_path'], C.get()['exp_name'])
    os.makedirs(base_path, exist_ok=True)
    add_filehandler(logger, os.path.join(base_path, '%s_%s_cv%.1f.log' % (augment['dataset'], augment['model_type'], args.cv_ratio)))
    augment['target_path'] = base_path + "/target_network.pt"
    augment['ctl_save_path'] = base_path + "/ctl_network.pt"
    start_t = time.time()
    controller = Controller(n_subpolicy=augment['num_policy'], lstm_size=augment['lstm_size'], emb_size=augment['emb_size'],
                            operation_prob=0)
    trace, train_metrics, test_metrics = train_ctl(controller, augment)
    aff_metrics = train_metrics["affinity"][-1]
    div_metrics = train_metrics["diversity"][-1]
    metrics = test_metrics[-1]
    gpu_secs = (time.time() - start_t) * torch.cuda.device_count()
    reporter(train_acc=div_metrics['top1'], affinity=aff_metrics["top1"], diversity=div_metrics["loss"], test_loss=metrics['loss'], test_acc=metrics['top1'], elapsed_time=gpu_secs, done=True)
    return metrics

@ray.remote(num_gpus=1, max_calls=1)
def train_model(config, dataloaders, dataroot, augment, cv_ratio_test, cv_id, save_path=None, skip_exist=False, evaluation_interval=5, gr_assign=None, gr_dist=None):
    C.get()
    C.get().conf = config
    C.get()['aug'] = augment
    result = train_and_eval(None, dataloaders, dataroot, cv_ratio_test, cv_id, save_path=save_path, only_eval=skip_exist, evaluation_interval=evaluation_interval, gr_assign=gr_assign, gr_dist=gr_dist)
    return C.get()['model']['type'], cv_id, result

if __name__ == '__main__':
    import json
    from pystopwatch2 import PyStopwatch
    w = PyStopwatch()

    parser = ConfigArgumentParser(conflict_handler='resolve')
    parser.add_argument('--dataroot', type=str, default='/mnt/hdd0/data/', help='torchvision data folder')
    parser.add_argument('--until', type=int, default=5)
    parser.add_argument('--num-op', type=int, default=2)
    parser.add_argument('--num-policy', type=int, default=5)
    parser.add_argument('--cv-num', type=int, default=5)
    parser.add_argument('--cv-ratio', type=float, default=0.4)
    parser.add_argument('--decay', type=float, default=-1)
    parser.add_argument('--redis', type=str)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--smoke-test', action='store_true')
    parser.add_argument('--random', action='store_true')
    parser.add_argument('--version', type=int, default=3)
    parser.add_argument('--childaug', type=str, default="clean")
    parser.add_argument('--load_search', action='store_true')
    parser.add_argument('--rand_search', action='store_true')
    parser.add_argument('--lstm_size', type=int, default=100)
    parser.add_argument('--emb_size', type=int, default=32)
    parser.add_argument('--c_lr', type=float, default=0.00035)
    parser.add_argument('--c_step', type=int)
    parser.add_argument('--a_step', type=int)
    parser.add_argument('--d_step', type=int)
    parser.add_argument('--cv_id', type=int)
    parser.add_argument('--M', type=int, default=1)
    parser.add_argument('--num-search', type=int, default=4)
    parser.add_argument('--validation', action='store_true')
    parser.add_argument('--search_name', type=str)

    args = parser.parse_args()
    torch.backends.cudnn.benchmark = True
    if args.decay > 0:
        logger.info('decay=%.4f' % args.decay)
        C.get()['optimizer']['decay'] = args.decay
    logger.info('configuration...')
    logger.info(json.dumps(C.get().conf, sort_keys=True, indent=4))
    cv_num = args.cv_num
    C.get()["cv_num"] = cv_num
    if 'test_dataset' not in C.get().conf:
        C.get()['test_dataset'] = C.get()['dataset']
    copied_c = copy.deepcopy(C.get().conf)
    paths = [_get_path(C.get()['dataset'], C.get()['model']['type'], '%s_ratio%.1f_fold%d' % (args.childaug, args.cv_ratio, i)) for i in range(cv_num)]
    logger.info('initialize ray...')
    ray.init(address=args.redis)
    logger.info('search augmentation policies, dataset=%s model=%s' % (C.get()['dataset'], C.get()['model']['type']))
    logger.info('----- Train without Augmentations cv=%d ratio(test)=%.1f -----' % (cv_num, args.cv_ratio))
    w.start(tag='train_no_aug')
    print(paths)
    reqs = [
        train_model.remote(copy.deepcopy(copied_c), None, args.dataroot, args.childaug, args.cv_ratio, i, save_path=paths[i], evaluation_interval=50)
        for i in range(cv_num)]

    tqdm_epoch = tqdm(range(C.get()['epoch']))
    is_done = False
    for epoch in tqdm_epoch:
        while True:
            epochs_per_cv = OrderedDict()
            for cv_idx in range(cv_num):
                try:
                    latest_ckpt = torch.load(paths[cv_idx])
                    if 'epoch' not in latest_ckpt:
                        epochs_per_cv['cv%d' % (cv_idx + 1)] = C.get()['epoch']
                        continue
                    epochs_per_cv['cv%d' % (cv_idx+1)] = latest_ckpt['epoch']
                except Exception as e:
                    continue
            tqdm_epoch.set_postfix(epochs_per_cv)
            if len(epochs_per_cv) == cv_num and min(epochs_per_cv.values()) >= C.get()['epoch']:
                is_done = True
            if len(epochs_per_cv) == cv_num and min(epochs_per_cv.values()) >= epoch:
                break
            time.sleep(10)
        if is_done:
            break
    logger.info('getting results...')
    pretrain_results = ray.get(reqs)
    aff_bases = []
    for r_model, r_cv, r_dict in pretrain_results:
        logger.info('model=%s cv=%d top1_train=%.4f top1_valid=%.4f' % (r_model, r_cv+1, r_dict['top1_train'], r_dict['top1_valid']))
        # for Affinity calculation
        aff_bases.append(r_dict['top1_valid'])
        del r_model, r_cv, r_dict
    logger.info('processed in %.4f secs' % w.pause('train_no_aug'))
    if args.until == 1:
        sys.exit(0)
    del latest_ckpt, pretrain_results, reqs
    ray.shutdown()
    logger.info('----- Search Test-Time Augmentation Policies -----')
    w.start(tag='search&train')

    ctl_config = {
            'dataroot': args.dataroot, 'split_ratio': args.cv_ratio, 'load_search': args.load_search, 'childnet_paths': paths,
            'lstm_size': args.lstm_size, 'emb_size': args.emb_size,
            'childaug': args.childaug, 'version': args.version, 'cv_num': cv_num, 'dataset': C.get()['dataset'],
            'model_type': C.get()['model']['type'], 'base_path': os.path.join(os.path.dirname(os.path.realpath(__file__)), 'models'),
            'ctl_train_steps': args.c_step, 'c_lr': args.c_lr, 'M': args.M, 'ctl_entropy_w': 1e-5,
            'num_policy': args.num_policy, 'validation': args.validation
            # 'cv_id': args.cv_id,
            # 'num_policy': args.num_policy,
            }
    if args.version == 2:
        space = {
                'mode': tune.choice(["ppo", "reinforce"]),
                'aff_step': tune.qloguniform(0, 2.21, 1),
                'div_step': tune.qloguniform(0, 2.6, 1),
                'ctl_num_aggre': tune.qloguniform(0, 2.6, 1),
                'cv_id': tune.choice([0,1,2,3,4,None]),
                }
        current_best_params = []
        # best result of cifar100-wideresnet-28-10
        # current_best_params = [{'mode': 0, 'aff_step': 2, 'div_step': 305, 'ctl_num_aggre': 19, 'cv_id': 0}, # 84.23
        #                        {'mode': 0, 'aff_step': 158, 'div_step': 391, 'ctl_num_aggre': 391, 'cv_id': 0} # 84.21
        #                        ]
        # best result of cifar10-wideresnet-28-10
        # current_best_params = [{'mode': 1, 'aff_step': 12, 'ctl_num_aggre': 162, 'div_step': 7, 'cv_id': 2}, # 97.61
        #                        {'mode': 0, 'aff_step': 10, 'ctl_num_aggre': 6, 'div_step': 204, 'cv_id': 3}, # 97.53
        #                        {'mode': 0, 'aff_step': 1, 'ctl_num_aggre': 1, 'div_step': 1, 'cv_id': None}, # 97.56
        #                        ]
    elif args.version == 3:
        # ctl_config['cv_id'] = args.cv_id
        ctl_config.update({
                'aff_step': args.a_step,
                'div_step': args.d_step,
                })
        space = {# search params
                'mode': "reinforce",
                # 'aff_w': tune.sample_from(lambda spec: round((np.random.beta(0.5, 0.5)+0.001)/1.002,3)),
                'aff_w': 0.5,
                'div_w': 0.5,
                'ctl_entropy_w': 1e-5,
                'reward_type': 4,
                'cv_id': tune.grid_search([0,1,2,3,4]),
                'num_policy': 2,
                }
        # space = {# search params
        #         'mode': "reinforce",
        #         'aff_w': tune.grid_search([0.99,0.9,0.5,0.1,0.01]),
        #         # 'div_w': tune.choice([0.99,0.9,0.5,0.1,0.01]),
        #         'reward_type': 4,
        #         'cv_id': 0,
        #         'num_policy': 2,
        #         # 'c_lr': tune.qloguniform(1e-4,1e-1,5e-5),
        #         }
        current_best_params = []
        # best result of cifar10-wideresnet-28-10
        # current_best_params = [{'mode': 1, 'aff_w': 3, 'div_w': 2, 'reward_type': 2, 'cv_id': 2, 'num_policy': 2}, # 0.9740 ['reinforce', 100.0, 1000.0, 3, 2, 5]
        #                        {'mode': 0, 'aff_w': 1, 'div_w': 3, 'reward_type': 0, 'cv_id': 2, 'num_policy': 2}, # 0.9734 ['ppo', 1.0, 10000.0, 1, 2, 5]
        #                         ]
        # best result of cifar100-wideresnet-28-10
        # current_best_params = [{'mode': 0, 'aff_w': 1, 'div_w': 3, 'reward_type': 0, 'cv_id': 0, 'num_policy': 1}, # 0.8423 ['ppo', 10.0, 1000.0, 1, 0, 2]
        #                        {'mode': 1, 'aff_w': 1, 'div_w': 3, 'reward_type': 2, 'cv_id': 0, 'num_policy': 1}] # 0.8422 ['reinforce', 10.0, 1000.0, 3, 0, 2]
    ctl_config.update(space)
    num_process_per_gpu = 1
    name = args.search_name
    reward_attr = 'test_acc'
    scheduler = AsyncHyperBandScheduler()
    register_trainable(name, lambda augment, reporter: train_ctl_wrapper(copy.deepcopy(copied_c), augment, reporter))
    algo = HyperOptSearch(points_to_evaluate=current_best_params)
    algo = ConcurrencyLimiter(algo, max_concurrent=num_process_per_gpu*torch.cuda.device_count())
    experiment_spec = Experiment(
        name,
        run=name,
        num_samples=args.num_search,
        stop={'training_iteration': args.num_policy},
        resources_per_trial={'cpu': 4, 'gpu': 1./num_process_per_gpu},
        config=ctl_config,
        local_dir=os.path.join(os.path.dirname(os.path.realpath(__file__)), "ray_results"),
        )
    analysis = run(experiment_spec, search_alg=None, scheduler=scheduler, verbose=1, queue_trials=True, resume=args.resume, raise_on_failed_trial=False,
                   progress_reporter=CLIReporter(metric_columns=[reward_attr], parameter_columns=list(space.keys())),
                   metric=reward_attr, mode="max", config=ctl_config)
    logger.info('getting results...')
    results = analysis.trials
    results = [x for x in results if x.last_result and reward_attr in x.last_result]
    results = sorted(results, key=lambda x: x.last_result[reward_attr], reverse=True)
    report = []
    for result in results:
        logger.info(f"affinity={result.last_result['affinity']:.4f} diversity={result.last_result['diversity']:.4f} test_acc={result.last_result['test_acc']:.4f}\
                    \n{ dict( (k, result.config[k]) for k in space ) }")
        report.append(result.last_result[reward_attr])
    logger.info(f"{reward_attr}: {100*np.mean(report):.3f}+-{100*np.std(report):.3f}")
    logger.info(w)
