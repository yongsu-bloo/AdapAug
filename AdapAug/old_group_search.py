import copy
import os
import sys
import time
from collections import OrderedDict, defaultdict

import torch

import numpy as np
from hyperopt import hp
import ray
import gorilla
from ray.tune.schedulers import AsyncHyperBandScheduler
from ray.tune.trial import Trial
from ray.tune.trial_runner import TrialRunner
from ray.tune.suggest.hyperopt import HyperOptSearch
from ray.tune.suggest import ConcurrencyLimiter
from ray.tune import register_trainable, run_experiments, run, Experiment
from tqdm import tqdm

from pathlib import Path
lib_dir = (Path("__file__").parent).resolve()
if str(lib_dir) not in sys.path: sys.path.insert(0, str(lib_dir))
from AdapAug.archive import remove_deplicates, policy_decoder, fa_reduced_svhn, fa_reduced_cifar10
from AdapAug.augmentations import augment_list
from AdapAug.common import get_logger, add_filehandler
from AdapAug.data_archive import old_get_dataloaders
from AdapAug.metrics import Accumulator, accuracy
from AdapAug.networks import get_model, num_class
from AdapAug.train import train_and_eval
from theconf import Config as C, ConfigArgumentParser
from AdapAug.group_assign import *
import csv
import warnings
warnings.filterwarnings("ignore")
top1_valid_by_cv = defaultdict(lambda: list)

def save_res(fname, *args):
    f = open(os.path.join(base_path, fname+"_bo_result.csv"), "a", newline="")
    wr = csv.writer(f)
    wr.writerow([*args])
    f.close()

def step_w_log(self):
    original = gorilla.get_original_attribute(ray.tune.trial_runner.TrialRunner, 'step')

    # log
    cnts = OrderedDict()
    for status in [Trial.RUNNING, Trial.TERMINATED, Trial.PENDING, Trial.PAUSED, Trial.ERROR]:
        cnt = len(list(filter(lambda x: x.status == status, self._trials)))
        cnts[status] = cnt
    best_top1_acc = 0.
    # last_acc = 0.
    for trial in filter(lambda x: x.status == Trial.TERMINATED, self._trials):
        if not trial.last_result:
            continue
        best_top1_acc = max(best_top1_acc, trial.last_result['top1_valid'])
        # last_acc = trial.last_result['top1_valid']
    # save_res(self._iteration, last_acc, best_top1_acc, cnts[Trial.TERMINATED])
    print('iter', self._iteration, 'top1_acc=%.3f' % best_top1_acc, cnts, end='\r')
    return original(self)


patch = gorilla.Patch(ray.tune.trial_runner.TrialRunner, 'step', step_w_log, settings=gorilla.Settings(allow_hit=True))
gorilla.apply(patch)


logger = get_logger('Fast AutoAugment')

def get_affinity(aug, aff_bases, config, augment):
    C.get()
    C.get().conf = config
    # setup - provided augmentation rules
    # C.get()['aug'] = aug
    load_paths = augment['load_paths']
    cv_num = augment["cv_num"]

    aug_loaders = []
    for cv_id in range(cv_num):
        _, tl, validloader, tl2 = old_get_dataloaders(C.get()['dataset'], C.get()['batch'], augment['dataroot'], augment['cv_ratio_test'], split_idx=cv_id, gr_assign=augment["gr_assign"], _transform=aug)
        aug_loaders.append(validloader)
        del tl, tl2

    loss_fn = torch.nn.CrossEntropyLoss(reduction='none')
    aug_accs = []
    for cv_id, loader in enumerate(aug_loaders):
        # eval
        model = get_model(C.get()['model'], num_class(C.get()['dataset']))
        ckpt = torch.load(load_paths[cv_id])
        if 'model' in ckpt:
            model.load_state_dict(ckpt['model'])
        else:
            model.load_state_dict(ckpt)
        model.eval()

        metrics = Accumulator()
        for data, label in loader:
            data = data.cuda()
            label = label.cuda()

            pred = model(data)
            loss = loss_fn(pred, label) # (N)

            _, pred = pred.topk(1, 1, True, True)
            pred = pred.t()
            correct = pred.eq(label.view(1, -1).expand_as(pred)).detach().cpu().numpy() # (1,N)

            # top1 = accuracy(pred, label, (1, 5))[0].detach().cpu().numpy()
            # correct = top1 * len(data)
            metrics.add_dict({
                'minus_loss': -1 * np.sum(loss.detach().cpu().numpy()),
                'correct': np.sum(correct),
                'cnt': len(data)
            })
            del loss, correct, pred, data, label
        aug_accs.append(metrics['correct'] / metrics['cnt'])
    del model
    affs = []
    for aug_valid, clean_valid in zip(aug_accs, aff_bases):
        affs.append(aug_valid - clean_valid)
    return affs

def _get_path(dataset, model, tag, basemodel=True):
    base_path = "models" if basemodel else f"models/{C.get()['exp_name']}"
    base_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), base_path)
    os.makedirs(base_path, exist_ok=True)
    return os.path.join(base_path, '%s_%s_%s.model' % (dataset, model, tag))     # TODO


@ray.remote(num_gpus=1, max_calls=1)
def train_model(config, dataloaders, dataroot, augment, cv_ratio_test, cv_id, save_path=None, skip_exist=False, gr_assign=None):
    warnings.filterwarnings("ignore")
    C.get()
    C.get().conf = config
    C.get()['aug'] = augment
    result = train_and_eval(None, dataloaders, dataroot, cv_ratio_test, cv_id, save_path=save_path, only_eval=skip_exist, gr_assign=gr_assign)
    return C.get()['model']['type'], cv_id, result

def eval_tta(config, augment, reporter):
    warnings.filterwarnings("ignore")
    C.get()
    C.get().conf = config
    cv_ratio_test, cv_id, save_path = augment['cv_ratio_test'], augment['cv_id'], augment['save_path']
    gr_id = augment["gr_id"]

    # setup - provided augmentation rules
    C.get()['aug'] = policy_decoder(augment, augment['num_policy'], augment['num_op'])

    # eval
    model = get_model(C.get()['model'], num_class(C.get()['dataset']))
    ckpt = torch.load(save_path)
    if 'model' in ckpt:
        model.load_state_dict(ckpt['model'])
    else:
        model.load_state_dict(ckpt)
    model.eval()

    loaders = []
    for _ in range(augment['num_policy']):  # TODO
        _, tl, validloader, tl2 = old_get_dataloaders(C.get()['dataset'], C.get()['batch'], augment['dataroot'], cv_ratio_test, split_idx=cv_id, gr_assign=augment["gr_assign"], target_lb=gr_id)
        loaders.append(iter(validloader))
        del tl, tl2

    start_t = time.time()
    metrics = Accumulator()
    loss_fn = torch.nn.CrossEntropyLoss(reduction='none')
    try:
        while True:
            losses = []
            corrects = []
            for loader in loaders:
                data, label = next(loader)
                data = data.cuda()
                label = label.cuda()

                pred = model(data)

                loss = loss_fn(pred, label) # (N)
                losses.append(loss.detach().cpu().numpy().reshape(1,-1)) # (1,N)

                _, pred = pred.topk(1, 1, True, True)
                pred = pred.t()
                correct = pred.eq(label.view(1, -1).expand_as(pred)).detach().cpu().numpy() # (1,N)
                corrects.append(correct)
                del loss, correct, pred, data, label

            losses = np.concatenate(losses)
            losses_min = np.min(losses, axis=0).squeeze() # (N,)

            corrects = np.concatenate(corrects)
            corrects_max = np.max(corrects, axis=0).squeeze() # (N,)
            metrics.add_dict({
                'minus_loss': -1 * np.sum(losses_min),
                'correct': np.sum(corrects_max),
                'cnt': corrects_max.size
            })
            del corrects, corrects_max
    except StopIteration:
        pass

    del model
    metrics = metrics / 'cnt'
    gpu_secs = (time.time() - start_t) * torch.cuda.device_count()
    reporter(minus_loss=metrics['minus_loss'], top1_valid=metrics['correct'], elapsed_time=gpu_secs, done=True)
    return metrics['correct']

def eval_tta2(config, augment, reporter):
    warnings.filterwarnings("ignore")
    C.get()
    C.get().conf = config
    cv_ratio_test, cv_id, save_path = augment['cv_ratio_test'], augment['cv_id'], augment['save_path']
    gr_id = augment["gr_id"]
    num_repeat = 1

    # setup - provided augmentation rules
    C.get()['aug'] = policy_decoder(augment, augment['num_policy'], augment['num_op'])

    # eval
    model = get_model(C.get()['model'], num_class(C.get()['dataset']))
    ckpt = torch.load(save_path)
    if 'model' in ckpt:
        model.load_state_dict(ckpt['model'])
    else:
        model.load_state_dict(ckpt)
    model.eval()

    loaders = []
    for i in range(num_repeat):
        _, tl, validloader, tl2 = old_get_dataloaders(C.get()['dataset'], C.get()['batch'], augment['dataroot'], cv_ratio_test, split_idx=cv_id, gr_assign=augment["gr_assign"], target_lb=gr_id)
        loaders.append(validloader)
        del tl, tl2


    start_t = time.time()
    metrics = Accumulator()
    loss_fn = torch.nn.CrossEntropyLoss(reduction='none')
    for loader in loaders:
        for data, label in loader:
            data = data.cuda()
            label = label.cuda()

            pred = model(data)
            loss = loss_fn(pred, label) # (N)

            _, pred = pred.topk(1, 1, True, True)
            pred = pred.t()
            correct = pred.eq(label.view(1, -1).expand_as(pred)).detach().cpu().numpy() # (1,N)

            metrics.add_dict({
                'minus_loss': -1 * np.sum(loss.detach().cpu().numpy()),
                'correct': np.sum(correct),
                'cnt': len(data)
            })
            del loss, correct, pred, data, label
    del model
    metrics = metrics / 'cnt'
    gpu_secs = (time.time() - start_t) * torch.cuda.device_count()
    reporter(minus_loss=metrics['minus_loss'], top1_valid=metrics['correct'], elapsed_time=gpu_secs, done=True)
    return metrics['correct']


if __name__ == '__main__':
    import json
    from pystopwatch2 import PyStopwatch
    w = PyStopwatch()

    parser = ConfigArgumentParser(conflict_handler='resolve')
    parser.add_argument('--dataroot', type=str, default='/mnt/hdd0/data/', help='torchvision data folder')
    parser.add_argument('--until', type=int, default=5)
    parser.add_argument('--num-op', type=int, default=2)
    parser.add_argument('--num-policy', type=int, default=5)
    parser.add_argument('--num-search', type=int, default=200)
    parser.add_argument('--cv-num', type=int, default=5)
    parser.add_argument('--cv-ratio', type=float, default=0.4)
    parser.add_argument('--decay', type=float, default=-1)
    parser.add_argument('--redis', type=str)
    parser.add_argument('--per-class', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--smoke-test', action='store_true')
    parser.add_argument('--exp_name', type=str)
    parser.add_argument('--gr-num', type=int, default=2)
    parser.add_argument('--random', action='store_true')
    parser.add_argument('--rpc', type=int, default=10)
    parser.add_argument('--version', type=int, default=1)
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--iter', type=int, default=5)
    parser.add_argument('--childaug', type=str, default="clean")
    parser.add_argument('--load_search', type=str)

    args = parser.parse_args()
    C.get()['exp_name'] = args.exp_name
    if args.decay > 0:
        logger.info('decay=%.4f' % args.decay)
        C.get()['optimizer']['decay'] = args.decay
    base_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'models', C.get()['exp_name'])
    os.makedirs(base_path, exist_ok=True)
    add_filehandler(logger, os.path.join(base_path, '%s_%s_cv%.1f.log' % (C.get()['dataset'], C.get()['model']['type'], args.cv_ratio)))
    logger.info('configuration...')
    logger.info(json.dumps(C.get().conf, sort_keys=True, indent=4))
    logger.info('initialize ray...')
    ray.init(address=args.redis)

    bench_policy_group = C.get()['aug']
    num_result_per_cv = args.rpc
    gr_num = args.gr_num
    gr_assign = None#gen_assign_group(version=args.version, num_group=gr_num)
    cv_num = args.cv_num
    C.get()["cv_num"] = cv_num
    copied_c = copy.deepcopy(C.get().conf)

    logger.info('search augmentation policies, dataset=%s model=%s' % (C.get()['dataset'], C.get()['model']['type']))
    logger.info('----- Train without Augmentations cv=%d ratio(test)=%.1f -----' % (cv_num, args.cv_ratio))
    w.start(tag='train_no_aug')
    paths = [_get_path(C.get()['dataset'], C.get()['model']['type'], '%s_ratio%.1f_fold%d' % (args.childaug, args.cv_ratio, i)) for i in range(cv_num)]
    print(paths)
    reqs = [
        train_model.remote(copy.deepcopy(copied_c), None, args.dataroot, args.childaug, args.cv_ratio, i, save_path=paths[i], skip_exist=True)
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
    logger.info('processed in %.4f secs' % w.pause('train_no_aug'))
    if args.until == 1:
        sys.exit(0)
    if args.load_search is None:
        logger.info('----- Search Test-Time Augmentation Policies -----')
        w.start(tag='search')
        ops = augment_list(False)
        space = {}
        for i in range(args.num_policy):
            for j in range(args.num_op):
                space['policy_%d_%d' % (i, j)] = hp.choice('policy_%d_%d' % (i, j), list(range(0, len(ops))))
                # space['prob_%d_%d' % (i, j)] = hp.uniform('prob_%d_ %d' % (i, j), 0.0, 1.0)
                space['level_%d_%d' % (i, j)] = hp.uniform('level_%d_ %d' % (i, j), 0.0, 1.0)

        num_process_per_gpu = 3
        total_computation = 0
        reward_attr = 'top1_valid'      # top1_valid or minus_loss
        # result_to_save = ['timestamp', 'top1_valid', 'minus_loss']
        final_policy_group = defaultdict(lambda : [])
        for _ in range(args.repeat):  # run multiple times.
            for gr_id in range(gr_num):
                for cv_id in range(cv_num):
                    final_policy_set = []
                    name = "search_%s_%s_group%d_%d_cv%d_ratio%.1f" % (C.get()['dataset'], C.get()['model']['type'], gr_id, gr_num, cv_id, args.cv_ratio)
                    print(name)
                    # bo_log_file = open(os.path.join(base_path, name+"_bo_result.csv"), "w", newline="")
                    # wr = csv.writer(bo_log_file)
                    # wr.writerow(result_to_save)
                    register_trainable(name, lambda augs, reporter: eval_tta2(copy.deepcopy(copied_c), augs, reporter))
                    algo = HyperOptSearch(space, metric=reward_attr, mode="max")
                    algo = ConcurrencyLimiter(algo, max_concurrent=20)
                    experiment_spec = Experiment(
                        name,
                        run=name,
                        num_samples=args.num_search,# if r == args.repeat-1 else 25,
                        resources_per_trial={'gpu': 1./num_process_per_gpu},
                        stop={'training_iteration': args.iter},
                        config = {
                                'dataroot': args.dataroot, 'save_path': paths[cv_id],
                                'cv_ratio_test': args.cv_ratio, 'cv_id': cv_id,
                                'num_op': args.num_op, 'num_policy': args.num_policy,
                                "gr_assign": gr_assign, "gr_id": gr_id
                                },
                        local_dir=os.path.join(base_path, "ray_results"),
                        )
                    analysis = run(experiment_spec, search_alg=algo, scheduler=AsyncHyperBandScheduler(), verbose=0, queue_trials=True, resume=args.resume, raise_on_failed_trial=False)
                    results = analysis.trials
                    print()
                    results = [x for x in results if x.last_result]
                    results = sorted(results, key=lambda x: x.last_result['timestamp'])
                    # for res in results:
                    #     wr.writerow([res.last_result[k] for k in result_to_save])
                    # bo_log_file.close()
                    results = sorted(results, key=lambda x: x.last_result[reward_attr], reverse=True)
                    # calculate computation usage
                    for result in results:
                        total_computation += result.last_result['elapsed_time']

                    for result in results[:num_result_per_cv]:
                        final_policy = policy_decoder(result.config, args.num_policy, args.num_op)
                        logger.info('loss=%.12f top1_valid=%.4f %s' % (result.last_result['minus_loss'], result.last_result['top1_valid'], final_policy))
                        final_policy = remove_deplicates(final_policy)
                        final_policy_set.extend(final_policy)
                    final_policy_group[gr_id].extend(final_policy_set)
                    # probs = []
                    # for aug in final_policy_set:
                    #     for op in aug:
                    #         probs.append(op[1])
                    # prob = sum(probs) / len(probs)
                    # print("mean_prob: {:.2f}".format(prob))
        final_policy_group = dict(final_policy_group)
        torch.save({
            "bench_policy": bench_policy_group,
            "final_policy": final_policy_group,
        }, base_path+"/search_summary.pt")
    else:
        search_load_path = args.load_search if os.path.exists(args.load_search) else base_path+"/search_summary.pt"
        search_info = torch.load(search_load_path)
        final_policy_group = search_info["final_policy"]
        logger.info(json.dumps(final_policy_group))
        logger.info("loaded search info from {}".format(search_load_path))
    logger.info(json.dumps(final_policy_group))
    logger.info('processed in %.4f secs, gpu hours=%.4f' % (w.pause('search'), total_computation / 3600.))
    logger.info('----- Train with Augmentations model=%s dataset=%s aug=%s ratio(test)=%.1f -----' % (C.get()['model']['type'], C.get()['dataset'], C.get()['aug'], args.cv_ratio))
    w.start(tag='train_aug')
    # g0 = fa_reduced_cifar10()
    # g1 = fa_reduced_svhn()
    num_experiments = torch.cuda.device_count() // 2
    default_path = [_get_path(C.get()['dataset'], C.get()['model']['type'], 'ratio%.1f_default%d' % (args.cv_ratio, _), basemodel=False) for _ in range(num_experiments)]
    augment_path = [_get_path(C.get()['dataset'], C.get()['model']['type'], 'ratio%.1f_augment%d' % (args.cv_ratio, _), basemodel=False) for _ in range(num_experiments)]
    loaders = old_get_dataloaders(C.get()['dataset'], C.get()['batch'], args.dataroot, 0.0, _transform=final_policy_group)
    reqs = [train_model.remote(copy.deepcopy(copied_c), None, args.dataroot, bench_policy_group, 0.0, 0, save_path=default_path[_], skip_exist=True, gr_assign=gr_assign) for _ in range(num_experiments)] + \
           [train_model.remote(copy.deepcopy(copied_c), loaders, args.dataroot, final_policy_group, 0.0, 0, save_path=augment_path[_], gr_assign=gr_assign) for _ in range(num_experiments)]

    tqdm_epoch = tqdm(range(C.get()['epoch']))
    is_done = False
    for epoch in tqdm_epoch:
        while True:
            epochs = OrderedDict()
            for exp_idx in range(num_experiments):
                try:
                    if os.path.exists(default_path[exp_idx]):
                        latest_ckpt = torch.load(default_path[exp_idx])
                        epochs['default_exp%d' % (exp_idx + 1)] = latest_ckpt['epoch']
                except:
                    pass
                try:
                    if os.path.exists(augment_path[exp_idx]):
                        latest_ckpt = torch.load(augment_path[exp_idx])
                        epochs['augment_exp%d' % (exp_idx + 1)] = latest_ckpt['epoch']
                except:
                    pass

            tqdm_epoch.set_postfix(epochs)
            if len(epochs) == num_experiments*2 and min(epochs.values()) >= C.get()['epoch']:
                is_done = True
            if len(epochs) == num_experiments*2 and min(epochs.values()) >= epoch:
                break
            time.sleep(10)
        if is_done:
            break

    logger.info('getting results...')
    final_results = ray.get(reqs)
    # Affinity Calculation
    augment = {
        'dataroot': args.dataroot, 'load_paths': paths,
        'cv_ratio_test': args.cv_ratio, "cv_num": args.cv_num,
        "gr_assign": gr_assign
    }
    bench_affs = get_affinity(bench_policy_group, aff_bases, copy.deepcopy(copied_c), augment)
    aug_affs = get_affinity(final_policy_group, aff_bases, copy.deepcopy(copied_c), augment)
    # Diversity calculation
    bench_divs = []
    aug_divs = []
    for train_mode in ['default','augment']:
        avg = 0.
        for _ in range(num_experiments):
            r_model, r_cv, r_dict = final_results.pop(0)
            logger.info('[%s] top1_train=%.4f top1_test=%.4f' % (train_mode, r_dict['top1_train'], r_dict['top1_test']))
            avg += r_dict['top1_test']
            if train_mode == 'default':
                bench_divs.append(r_dict['loss_train'])
            else:
                aug_divs.append(r_dict['loss_train'])
        avg /= num_experiments
        logger.info('[%s] top1_test average=%.4f (#experiments=%d)' % (train_mode, avg, num_experiments))
    torch.save({
        "aug_affs": aug_affs,
        "aug_divs": aug_divs,
        "bench_affs": bench_affs,
        "bench_divs": bench_divs
    }, base_path+"/summary.pt")

    logger.info('processed in %.4f secs' % w.pause('train_aug'))
    logger.info("bench_aff_avg={:.2f}".format(np.mean(bench_affs)))
    logger.info("aug_aff_avg={:.2f}".format(np.mean(aug_affs)))
    logger.info("bench_div_avg={:.2f}".format(np.mean(bench_divs)))
    logger.info("aug_div_avg={:.2f}".format(np.mean(aug_divs)))
    logger.info(w)
