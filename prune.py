import torch
from torch.autograd import Variable
from torchvision import models
import cv2
import sys
import numpy as np
 
def replace_layers(model, i, indexes, layers):
    """
    if i in indexes, then layer i will i replaced by layers[i]
    
    Parameters:
        model: torch.Module; model or sub model(sub graph).
	i: int; the layer index of pruned (sub)model which will be returned.
	index: list with 2 int; pruning target layer index and next layer index.
	layers: the pruned layer which will replace corresponding layer in model.

    Returns:
        torch.nn.layers: certain layer of pruned model.
    """
    if i in indexes:
        return layers[indexes.index(i)]
    return model[i]

def prune_vgg16_conv_layer(model, layer_index, filter_index):
        """
	pruning vgg16 model.
	
	Parameters:
	    model: torch.nn.Net; model.
	    layer_index: int; layer index.
	    filter_index: int; the index of the channel which will be pruned.
	"""
	""" extracting the current conv layer """
	_, conv = model.features._modules.items()[layer_index]
	next_conv = None
	""" offset = 1:
	means the next layer is the layer just next to current layer, 
	but ofcourse, the offset value is dynamic.
	"""
	offset = 1

	"""
	while layer_index + offset <  len(model.features._modules.items()):
	the reason is make sure the pruning conv layer is not the last conv
	layer which is connecting with full connect layer.
	
	in following code block, it will dynamically change the offset value
	to locate the nearest next conv layer.
	"""
	while layer_index + offset < len(model.features._modules.items()):
		""" res next layer base on current offset. """
		res =  model.features._modules.items()[layer_index + offset]
		if isinstance(res[1], torch.nn.modules.conv.Conv2d):
			next_name, next_conv = res
			break
		offset = offset + 1
	
	""" 
	?: why minus one. 
	because this operation will cut one
	channel in later, so the channels' number minus one. 
	
	the variable new_conv can be understood as a initialize or cache 
	of the pruned current conv layer.
	"""
	new_conv = \
	    torch.nn.Conv2d(
		in_channels = conv.in_channels, 
		out_channels = conv.out_channels - 1, 
		kernel_size = conv.kernel_size, 
		stride = conv.stride,
		padding = conv.padding,
		dilation = conv.dilation,
		groups = conv.groups,
		bias = conv.bias
            )

	""" cache old weight and initialize new weight. """
	""" drawbacks: converting values to numpy.ndarray so can not use gpu """
	old_weights = conv.weight.data.cpu().numpy()
	new_weights = new_conv.weight.data.cpu().numpy()

	""" 
	# 1: re-loading the weights value which channel number located before 
	     the waiting pruning channel.
	# 2: re-loading the weights value which channel number located after 
	     the waiting pruning channel.	
	comments: we can do this more beautiful.
	"""
	""" filter index which will handeling the pruning layer. """
	new_weights[:filter_index, :, :, :] = old_weights[:filter_index, :, :, :] # 1
	new_weights[filter_index: , :, :, :] = old_weights[(filter_index + 1):, :, :, :] # 2
	new_conv.weight.data = torch.from_numpy(new_weights).cuda()

	""" cache bias. """
	bias_numpy = conv.bias.data.cpu().numpy()

	""" re-initialize bias with zeros. """ 
	bias = np.zeros(shape = (bias_numpy.shape[0] - 1), dtype = np.float32)
	
	""" same operation with how to handel weights above. """
	bias[:filter_index] = bias_numpy[:filter_index]
	bias[filter_index:] = bias_numpy[filter_index + 1:]
	new_conv.bias.data = torch.from_numpy(bias).cuda()

	"""
	since just pruned(handeled) current conv layer( to new_conv), 
	next conv layer should also be handeled corresponding to this operation, 
	in this case, the in_channels but not out_channels should minus 1.
	"""
	if not next_conv is None:
	    """ initialize pruned next conv layer. """
	    next_new_conv = torch.nn.Conv2d(
	        in_channels = next_conv.in_channels - 1,
		out_channels =  next_conv.out_channels, 
		kernel_size = next_conv.kernel_size, 
		stride = next_conv.stride,
		padding = next_conv.padding,
		dilation = next_conv.dilation,
		groups = next_conv.groups,
		bias = next_conv.bias
	    )
	    """ cache & initialize weights values for next conv layer. """
	    old_weights = next_conv.weight.data.cpu().numpy()
	    new_weights = next_new_conv.weight.data.cpu().numpy()

	    """ similiar with above. """
	    new_weights[:, :filter_index, :, :] = old_weights[:, :filter_index, :, :]
	    new_weights[:, filter_index: , :, :] = old_weights[:, (filter_index + 1):, :, :]
            next_new_conv.weight.data = torch.from_numpy(new_weights).cuda()
            """ cache bias value. """
	    next_new_conv.bias.data = next_conv.bias.data

	if not next_conv is None:
	    """ this code block does:
	    First do pruning operation layer by layer, after than combine these pruned layers 
	    together and build the variable features to replace the attribute "features" in 
	    model.
	    This operation will only happen "if not next_conv is None", which means the current 
	    conv layer is not last conv layer.
	    """
	    features = torch.nn.Sequential(
	        *(replace_layers(model.features, i, [layer_index, layer_index+offset], \
	          new_conv, next_new_conv]) for i, _ in enumerate(model.features))
	    )
	    del model.features
	    del conv
	    model.features = features

	else:
	    #Prunning the last conv layer. This affects the first linear layer of the classifier.
	    model.features = torch.nn.Sequential(
	        *(replace_layers(model.features, i, [layer_index], \
	          [new_conv]) for i, _ in enumerate(model.features))
	    )
	    layer_index = 0
	    old_linear_layer = None
	    for _, module in model.classifier._modules.items():
	        if isinstance(module, torch.nn.Linear):
	 	    old_linear_layer = module
	 	    break
	 	    layer_index = layer_index  + 1

	 	if old_linear_layer is None:
	 	    raise BaseException("No linear laye found in classifier")
		
		params_per_input_channel = old_linear_layer.in_features / conv.out_channels

	 	new_linear_layer = torch.nn.Linear(
		    old_linear_layer.in_features - params_per_input_channel, 
	 	    old_linear_layer.out_features
		)
	 	
	 	old_weights = old_linear_layer.weight.data.cpu().numpy()
	 	new_weights = new_linear_layer.weight.data.cpu().numpy()	 	

	 	new_weights[:, : filter_index * params_per_input_channel] = \
	 		old_weights[:, : filter_index * params_per_input_channel]
	 	new_weights[:, filter_index * params_per_input_channel :] = \
	 		old_weights[:, (filter_index + 1) * params_per_input_channel :]
	 	
	 	new_linear_layer.bias.data = old_linear_layer.bias.data

	 	new_linear_layer.weight.data = torch.from_numpy(new_weights).cuda()

		classifier = torch.nn.Sequential(
			*(replace_layers(model.classifier, i, [layer_index], \
				[new_linear_layer]) for i, _ in enumerate(model.classifier)))

		del model.classifier
		del next_conv
		del conv
		model.classifier = classifier

	return model

if __name__ == '__main__':
	model = models.vgg16(pretrained=True)
	model.train()

	t0 = time.time()
	model = prune_conv_layer(model, 28, 10)
	print "The prunning took", time.time() - t0
