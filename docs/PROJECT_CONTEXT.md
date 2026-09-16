This document is a description of the project context and the role of the GUI within it.

## What is AUTOMATA project

AUTOMATA is a project financiated by the European Union that involves various companies and universities, the aim is to create a machine that automatize the analysis of archeological objects. This machine will be used by museums, universities and research institutions to create a database of digital twins for pottery pieces and lithics.
The operator will load the machine with a gridded box, where the samples are placed, and the system will perform 3D Imaging (3DI), Hyper Spectral Imaging (HSI), uXRF measures and Raman measures. All this data is stored locally on the machine; the local database is updated manually (there is no automatic cloud upload).

## Design of the AUTOMATA machine

Going a little bit further in the physical structure of the machine, it is composed  by an area where the box will be placed (with a camera); then a first robotic arm of with a costumized end effector for pick-and-place, XRF and Raman analysis; then a rotating platform with 2 transparent disks; then a second robotic arm with a costumized end effector for integrating the HSI and 3DI.

## The workflow is the following:

1. Human operator loads the gridded box with samples into the machine.
2. Vision system identify the objects, create IDs, create analyzing order.
3. Box loop starts. 
4. First Robot and picks up the target object.
5. The object is placed on the transparent disk in first position.
6. The platform rotated and the disk with the object arrives in the second position.
7. The Second Robot and performs 3DI and then HSI on the object. {First Robot picks up the next object and put on the other transparent disk}
8. An AI system uses that data in order to extrapolate interest points on the object for next puntual measurements.
9. The platform rotated and the disk with the object goes back in the first position. {The next object on the other disk goes in second position}
10. First Robot performs XRF and Raman measurements on the AI's choosen points. {The Second Robot performs 3DI and then HSI on the other object}
11. First Robot, picks the object from disk and put it back in its original place inside box and picks the next target object from the box.
12. Repeat untill all object in the box have been analyzed.

This workflow allows to cut time because the 2 robots are working in parallel. This workflow has been organized in order that 3DI and HSI are performed before XRF and Raman becouse this last two analysis are puntual and the points are decided after analyzing 3DI and HSI data. Also this split of duty between the 2 robots follows a timing logic: {XRF + Raman + 'Put back object in box' + 'Pick a new object from box'} has a similar execution time of {3DI + HSI + 'AI point extrapolation'} 

During the workflow the human operator can insert alias IDs from the GUI to the objects in the box, this IDs will be used for the local database (the machine is already working in the meanwhile with the automatically assigned IDs).
The data is stored locally on the machine for each object completed; the local database is updated manually using the alias IDs.

## Software Architecture

The System is distribuited on different computers (Ubuntu and Windows involved): all the sub-systems communicate with a GUI orchestrator (the code of this repo) through MQTT.

The sub-systems are:

| Subsystem | Hardware Involved | Description | Software Framework
|---|---|---|---|
| GUI orchestrator | Ubuntu PC, Touch screen | central GUI application, workflow orchestration, MQTT messaging | NodeJs |
| Vision system | RGB-D camera, gridded box, lighting | object detection, ID creation, analysis order generation | Docker, Python application | 
| First robotic arm | Robot arm, custom end effector (gripper+XRF+Raman) | pick-and-place, XRF/Raman measurement positioning and execution | Docker, ROS2 nodes |
| Rotating platform | motorized rotary stage, transparent disks | object indexing between measurement positions | Digital I/Os interface |
| Second robotic arm | Robot arm, custom end effector (HSI + 3DI)| HSI and 3DI trajectory of scanning execution | Docker, ROS2 nodes |
| XRF interface | XRF analyzer hardware | manage communication, trigger acquisition, and transfer data from XRF device | ? |
| Raman interface | Raman analyzer hardware | manage communication, trigger acquisition, and transfer data from Raman device | ? |
| 3DI interface | 3DI imaging module | manage communication, synchronization, and data acquisition from the 3DI device (performed first) | ? |
| HSI interface | HSI imaging module | manage communication, synchronization, and data acquisition from the HSI device (performed after 3DI) | ? |
| AI analysis | local compute server | interest point extraction, measurement planning for XRF/Raman | ? |

## GUI design principles

The GUI orchestrate and interface the system with operator, it follows an interaction hierarchy based on State Machine design. 

From highest to lowest level:

1. **Human Operator** using the Graphical Interface directly acts on **System State Machine** ( Start, Stop, Pause, Perform Required Action).
2. **System State Machine** acts and reacts on **Sub-systems State Machines**.
3. **Sub-systems State Machines** are multiple entities, each one reads and sends data to the actual sub-system via MQTT communication. 

All the state machines are defined by a configuration yaml file. The rules for this configuration files are described in the file [docs/STATE_MACHINE.md](STATE_MACHINE.md)
