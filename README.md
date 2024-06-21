## config.yaml
* camera: адрес камеры \ 0 для дефолтной
* source: excel \ db
* excel_file: если source == excel
* images_folder: папка с фото пользователей
* frame_folder: папка с  кадрами  распознаных в момент прохода
* mode: facenet (https://github.com/timesler/facenet-pytorch) / face-recognition (https://github.com/ageitgey/face_recognition)
* no_name_user: как подписываются неизвестные лица
* show: True \ False - включить вывод на экран
* test_mode: True \ False - в тестовом режиме турникеты не открываются
* date_time_format: формат вывода времени для включение его в имя файла
* log_file: имя лог файла

* connection:
  + host: адрес хоста, упарвляющего дверьми
  + login
  + password

* face-recognition:
  + embedding_folder: папка, где хранятся эмбеддинги 
  + num_jitters
  + tolerance: чем выше, тем строже опознавание

* facenet:
  + embedding_folder: папка с эмбеддингами
  + threshold: чем ниже, тем строже

* tourniquets:
  + area_1: x, y, w, h зоны 1 в относительных значениях
  + area_2
  + id_tur  для тугникета Perco

Чтобы создать эмбеддинги, запустить с опцией -e 
