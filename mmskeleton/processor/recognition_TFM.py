from collections import OrderedDict
import torch
import logging
import numpy as np
from mmskeleton.utils import call_obj, import_obj, load_checkpoint, save_checkpoint
from mmcv.runner import Runner
from mmcv import Config, ProgressBar
from mmcv.parallel import MMDataParallel


def test(model_cfg,
         dataset_cfg,
         checkpoint,
         batch_size=None,
         gpu_batch_size=None,
         gpus=-1,
         workers=4):

    # calculate batch size
    if gpus < 0:
        gpus = torch.cuda.device_count()
    if (batch_size is None) and (gpu_batch_size is not None):
        batch_size = gpu_batch_size * gpus
    assert batch_size is not None, 'Please appoint batch_size or gpu_batch_size.'

    dataset = call_obj(**dataset_cfg)
    data_loader = torch.utils.data.DataLoader(dataset=dataset,
                                              batch_size=batch_size,
                                              shuffle=False,
                                              num_workers=workers,
                                              timeout=0)

    # put model on gpus
    if isinstance(model_cfg, list):
        model = [call_obj(**c) for c in model_cfg]
        model = torch.nn.Sequential(*model)
    else:
        model = call_obj(**model_cfg)
    load_checkpoint(model, checkpoint, map_location='cpu')
    model = MMDataParallel(model, device_ids=range(gpus)).cuda()
    model.eval()

    results = []
    labels = []
    prog_bar = ProgressBar(len(dataset))
    for data, label in data_loader:
        with torch.no_grad():
            output = model(data)
            output = model(data).data.cpu().numpy()

        results.append(output)
        labels.append(label)
        for i in range(len(data)):
            prog_bar.update()
    results = np.concatenate(results)
    labels = np.concatenate(labels)

    for s,l in zip(results, labels):
        print(s,l,s-l)

    print('Accuracy:', accuracy(torch.from_numpy(results), torch.from_numpy(labels)))

    if 'output_path' in dir(dataset):
        import os
        dirname = os.path.join(dataset.output_path, 'output')
        if not os.path.isdir(dirname):
            os.mkdir(dirname)

        f = open(os.path.join(dirname, 'results_' + os.path.basename(dataset.data_path)), 'wb')
        np.save(f, np.asarray( (results,labels) ) )
        f.close()

        import csv
        f = open(os.path.join(dirname, 'results_' + os.path.basename(dataset.data_path) + '.csv'), 'w')
        writer = csv.writer(f)

        for s,l in zip(results, labels):
            aux = s.tolist()
            aux.extend(l.tolist())
            writer.writerow( aux )
        
        f.close()

def train(
        work_dir,
        model_cfg,
        loss_cfg,
        dataset_cfg,
        optimizer_cfg,
        total_epochs,
        training_hooks,
        batch_size=None,
        gpu_batch_size=None,
        workflow=[('train', 1)],
        gpus=-1,
        log_level=10,
        workers=4,
        resume_from=None,
        load_from=None,
        save_to=None
):

    # calculate batch size
    if gpus < 0:
        gpus = torch.cuda.device_count()
    if (batch_size is None) and (gpu_batch_size is not None):
        batch_size = gpu_batch_size * gpus
    assert batch_size is not None, 'Please appoint batch_size or gpu_batch_size.'

    # prepare data loaders
    if isinstance(dataset_cfg, dict):
        dataset_cfg = [dataset_cfg]
    data_loaders = [
        torch.utils.data.DataLoader(dataset=call_obj(**d),
                                    batch_size=batch_size,
                                    shuffle=True,
                                    num_workers=workers,
                                    drop_last=True) for d in dataset_cfg
    ]

    # put model on gpus
    if isinstance(model_cfg, list):
        model = [call_obj(**c) for c in model_cfg]
        model = torch.nn.Sequential(*model)
    else:
        model = call_obj(**model_cfg)
    model.apply(weights_init)

    model = MMDataParallel(model, device_ids=range(gpus)).cuda()
    loss = call_obj(**loss_cfg)

    # build runner
    optimizer = call_obj(params=model.parameters(), **optimizer_cfg)

    # Print model's state_dict
    print("Model's state_dict:")
    for param_tensor in model.state_dict():
        print(param_tensor, "\t", model.state_dict()[param_tensor].size())

    # Print optimizer's state_dict
    print("Optimizer's state_dict:")
    for var_name in optimizer.state_dict():
        print(var_name, "\t", optimizer.state_dict()[var_name])

    runner = Runner(model, batch_processor, optimizer, work_dir, log_level)
    runner.register_training_hooks(**training_hooks)

    if resume_from:
        runner.resume(resume_from)
    elif load_from:
        runner.load_checkpoint(load_from)

    # run
    workflow = [tuple(w) for w in workflow]
    runner.run(data_loaders, workflow, total_epochs, loss=loss)

    if save_to:
        print("Saving checkpoint to", save_to)
        save_checkpoint(model, save_to)


# process a batch of data
def batch_processor(model, datas, train_mode, loss):

    data, label = datas
    data = data.cuda()
    label = label.cuda()

    # forward
    output = model(data)
    losses = loss(output, label)

    # output
    log_vars = dict(loss=losses.item())
    if not train_mode:
        log_vars['acc'] = accuracy(output, label)

    outputs = dict(loss=losses, log_vars=log_vars, num_samples=len(data.data))
    return outputs


def accuracy(score, label):
    loss = torch.nn.L1Loss()
    output = loss(score, label)
    return output.cpu().numpy()


def weights_init(model):
    classname = model.__class__.__name__
    if classname.find('Conv1d') != -1:
        model.weight.data.normal_(0.0, 0.02)
        if model.bias is not None:
            model.bias.data.fill_(0)
    elif classname.find('Conv2d') != -1:
        model.weight.data.normal_(0.0, 0.02)
        if model.bias is not None:
            model.bias.data.fill_(0)
    elif classname.find('BatchNorm') != -1:
        model.weight.data.normal_(1.0, 0.02)
        model.bias.data.fill_(0)
